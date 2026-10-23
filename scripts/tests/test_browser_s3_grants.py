# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The BROWSER may read nothing from S3 with the signed-in user's own credentials.

``CognitoIdentityPoolSetRole`` attaches a **single** ``authenticated`` role with no
``RoleMappings``, so Cognito group membership plays no part in which role a signed-in
user assumes. Any bucket that role named would therefore be readable by every
authenticated user irrespective of group — including one in no group, which
self-signup produces — with **no resolver in the path**, so no group check, no
per-document check and no per-user scope check would be available to apply to it.
That is what made this one IAM policy, rather than the API surface, the boundary for
everything it named, and it is why the policy belongs under a gate rather than under
review.

**The role now grants no S3 at all, and this file pins that.** It used to grant
``s3:GetObject`` and ``s3:GetObjectVersion`` on the Input and Output buckets, which is
"read any document's bytes by key, as any signed-in account" — the two buckets hold
every uploaded source document and every document's extracted output, page images and
OCR text. Issue
[#1033](https://github.com/aws-solutions-library-samples/accelerated-intelligent-document-processing-on-aws/issues/1033)
measured that end to end against a live stack. The document read path now goes through
``getFilePresignedUrl`` / ``getFilePresignedUrls``, where the dispatcher's field-level
group floor, the resolver's bucket allow-list and ``document_scope``'s per-document
check all apply.

So the assertions here are the **inverse** of what they were. The previous version
pinned a residual — a named set of buckets that *were* readable — and its job was to
stop that set growing. This version asserts the set is empty, which is a stronger
claim and a more fragile one: four of the five old assertions pass trivially once
there is no statement to read, so most of this file is now about **not being
vacuous**.

Three things are pinned, in different directions.

**No S3 statement, under any policy name.** Collected across all of the role's inline
policies rather than the one that used to be called ``S3``: a grant added under
another name would be just as effective.

**No KMS statement either.** Not an S3 action, but it was in the same policy and for
the same consumer — ``kms:Decrypt`` so a client-presigned GET could decrypt an
SSE-KMS object. The browser has no KMS client, this role never held
``s3:PutObject``, and ``SettingsParameter`` is a plain ``Type: String`` parameter, so
nothing else needs it. Pinned by asserting the role's whole policy set rather than by
naming KMS, because the question worth asking is "what else did this role acquire",
and a set comparison answers it for every service at once.

**There is more than one way for an S3 read to reach the browser, and the reason
this file enumerates them is that every one of them was green at some point while
the grant was live.** Seven routes, grouped by what they attach to what:

*A policy reaching the role.* Inline under the role's own ``Policies``; a standalone
``AWS::IAM::Policy`` or ``AWS::IAM::ManagedPolicy`` naming the role in ``Roles`` — an
idiom this template already uses twice, to break circular dependencies, and one
nothing in the role resource mentions; an ``AWS::IAM::RolePolicy``, which is the same
idiom one property name away (singular ``RoleName``), so a reader matching ``Roles``
misses it; and ``ManagedPolicyArns``, the one route a gate cannot evaluate at all,
because the policy lives outside this template — so the only sound assertion there is
that the list is empty.

*A grant hiding inside a statement the reader skims.* An action spelled
``S3:GetObject`` (IAM action names are case-insensitive, so ``startswith("s3:")``
misses a live grant), and ``NotAction`` — ``{"Effect": "Allow", "NotAction":
["iam:*"], "Resource": "*"}`` grants more than #1033 removed and names no action at
all. Both can sit inside a policy whose *name* is already expected, so the policy-set
pin does not fire either.

*The role itself not being the role.* ``CognitoIdentityPoolSetRole`` decides which
role the browser assumes, and :data:`ROLE` is a literal — so a second role carrying
the old policy, with the attachment repointed at it, left both gates reading a role
nobody assumes. The attachment is now derived and compared, and ``RoleMappings`` is
asserted absent because "every signed-in user holds this irrespective of group" is a
consequence of there being none.

*The other side of the authorization.* For a same-account caller S3 needs an explicit
``Allow`` in **either** the identity policy or the bucket policy, so an identity
policy granting nothing does not bound what the role can read if an
``AWS::S3::BucketPolicy`` names its principal.

The premise chain, and what asserts each link
---------------------------------------------
Twelve routes past this gate were found over five review rounds, each one level above
the last, and the useful product of that is not the twelve fixes — it is knowing what
the claim actually rests on. Written out, so the next change is checked against named
premises rather than against somebody's imagination, and so a thirteenth route shows
up as **a premise with no assertion attached** rather than as a probe nobody thought
to run:

=== ======================================================= ==========================
#   Premise                                                 Asserted by
=== ======================================================= ==========================
1   The browser obtains credentials only from this           ⚠️ **nothing**
    Identity Pool
2   The pool hands exactly one role to signed-in users,      a single ``authenticated``
    and none to anyone else                                  value naming exactly one
                                                             role, no ``RoleMappings``,
                                                             no ``unauthenticated``
                                                             entry, and the
                                                             unauthenticated flag a
                                                             literal ``false``
3   That role's effective permissions are its inline         all four, each read and
    policies, standalone attachments, managed policies        each refusing a shape it
    and resource-based policies naming it                     cannot evaluate
4   All of those are declared in ``template.yaml``            no nested stack is passed
                                                             the role; no ``Resources``
                                                             entry without a literal
                                                             ``Type``
5   ``template.yaml`` as committed is what is deployed        ⚠️ **nothing** — see below
=== ======================================================= ==========================

Every one of the twelve routes lived in links 2 and 3. Both are closed. Links 1 and 5
are the honest residuals, and link 5 is the one with a measured reason to doubt it.

⚠️ **The deployed artifact is not this file.** ``lib/idp_sdk/.../template_transform.py``
rewrites templates for the ``--headless`` and ``--govcloud`` variants, and nothing here
reads its output — ``browser_s3_policy`` is imported only by this file and by
``test_iam_prose_consistency.py``. ``HeadlessTemplateTransformer`` deletes
:data:`ROLE` outright, so there the claim is vacuously true; the GovCloud transformer
deliberately keeps the UI, so the role survives into that artifact. The comfortable
assumption is that a transform can only *narrow* a policy, and that is **false**: the
same module has an additive path into a resource's ``Policies``
(``policies.append({"DynamoDBCrudPolicy": ...})``). It targets Lambda functions rather
than this role, so there is no live defect — but that makes the inheritance rest on
"the additive path happens not to touch this role", which is exactly the shape of
premise this gate has been wrong about four times. The cheap form of the missing
assertion is a property of the transformer (no transformer may add to the ``Policies``
of the resource named by :data:`ROLE`); the thorough form is running these assertions
over the GovCloud-transformed template, which ``scripts/generate_govcloud_template.py``
already produces offline.

**The reader is not broken.** Every green assertion below is also satisfied by a
parser that has stopped finding its subject — a renamed role, a restructured
``Policies`` block, a ``Fn::GetAtt`` idiom the flattener drops, an action spelled
``S3:GetObject`` (IAM action names are case-insensitive; a ``startswith("s3:")`` test
is not). So the role and its policy set are asserted positively, and the statement
collector and the resource matcher are each driven over synthetic policies that *do*
grant S3 — through both attachment routes — so a collector that can no longer see a
grant fails here rather than reporting the tree clean.
"""

from __future__ import annotations

import re

import pytest

# The policy parser is shared with `test_iam_prose_consistency.py`, which cross-checks
# the documents against the same derived set. Two copies of a policy reader diverge,
# and the copy that diverges is the one nobody re-reads.
from browser_s3_policy import (
    DOCUMENT_BUCKETS,
    ROLE,
    ROLE_ATTACHING_TYPES,
    UnreadablePolicyShape,
    authenticated_role_logical_id,
    bucket_policies_reaching_documents,
    identity_pool_allows_unauthenticated,
    identity_pool_role_mappings,
    identity_pool_unauthenticated_roles,
    nested_stacks_given_the_role,
    policy_names_role,
    principal_may_include_role,
    role_attached_policies,
    role_managed_policy_arns,
    role_policies,
    role_s3_statements,
    statement_reaches_s3,
)
from browser_s3_policy import buckets_named as _buckets_named
from browser_s3_policy import collect_s3_statements as _collect_s3_statements
from browser_s3_policy import is_wildcard_resource as _is_wildcard_resource
from browser_s3_policy import resource_strings as _resource_strings

pytestmark = pytest.mark.unit


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# The policies this role may carry, with one member's worth of reason each. A set
# rather than a minimum, so a grant of ANY service added here fails — which is how
# the removed KMS statement stays removed without this file having to know what KMS
# is. Not an exemption list: nothing in it is excused from an assertion, and the
# membership rule is "the browser genuinely performs this call with its own
# credentials, and the grant cannot express a per-user boundary because it does not
# need to".
EXPECTED_ROLE_POLICIES = {
    "ChatStreamInvoke": (
        "lambda:InvokeFunction on the chat-streaming Function URL, which the browser "
        "invokes with SigV4 under AuthType=AWS_IAM. The identity-side half of that "
        "grant has to be on this role; there is no per-user axis it could express, "
        "and the group check for a chat turn is in the component that does the work "
        "(see GAP-07 / AUTH.T14 for the residual)"
    ),
    "ParameterStore": (
        "ssm:GetParameter on this stack's single Settings parameter, which the UI "
        "reads at start-up for bucket names, the API endpoint and feature flags. "
        "Deployment-wide configuration with no per-user content, and one named "
        "parameter rather than a prefix"
    ),
}

# Buckets that must never be reachable with the browser's own credentials, and why.
# One entry per bucket, each carrying the axis an IAM grant on a single shared role
# cannot express. Every bucket in the stack is now in this position, so the list is
# not the enforcement — `test_the_role_grants_no_s3_at_all` is — but these four are
# the ones whose contents make the rule worth stating, and naming them keeps the
# reason attached to the member rather than to the set.
BROWSER_FORBIDDEN_BUCKETS = {
    "ConfigurationBucket": (
        "holds every configuration profile's revision history under "
        "config_revisions/<profile>/; visibility is the caller's per-user "
        "allowedConfigVersions, enforced on the key by "
        "get_file_contents_resolver/key_scope.py"
    ),
    "TestSetBucket": (
        "holds each test set's source documents and ground truth under "
        "<test_set_id>/; visibility is an Annotator's per-user allowedTestSets, "
        "enforced on the key by get_file_contents_resolver/key_scope.py"
    ),
    "InputBucket": (
        "holds every uploaded source document, keyed by the document's own "
        "ObjectKey; visibility is the caller's allowedConfigVersions matched "
        "against the document's ConfigVersion, enforced by "
        "get_file_contents_resolver/document_scope.py"
    ),
    "OutputBucket": (
        "holds every document's extracted output, page images and OCR text under "
        "<ObjectKey>/; same per-document axis, enforced in the same place"
    ),
}


@pytest.fixture(scope="module")
def s3_statements():
    """Every S3 statement in the authenticated role's inline policies.

    Expected to be empty. The fixture stays because the assertions that read it are
    the ones that would catch a grant coming back.
    """
    return role_s3_statements()


@pytest.fixture(scope="module")
def policies():
    """``{policy_name: policy_document}`` for every inline policy on the role."""
    return role_policies()


def test_the_gate_still_finds_the_role_and_its_policies(policies):
    """Otherwise every assertion below passes vacuously.

    This is the whole of the non-vacuity story for the *template* side: with the
    grant gone, "the role declares no S3 statement" is also what a parser reading
    the wrong resource reports. A renamed role or a restructured ``Policies`` block
    fails here instead of reading as a tightening.
    """
    assert policies, (
        f"{ROLE} declares no inline policies at all, so every assertion in this file "
        "is reading an empty set. Either the role was renamed or restructured, or "
        "browser_s3_policy has stopped finding it — fix the reader, do not take the "
        "green marks below."
    )


def test_every_policy_reaching_this_role_is_readable():
    """Named first, because an unreadable shape makes every assertion below unsound.

    The readers raise :class:`UnreadablePolicyShape` rather than skipping what they
    cannot evaluate, so an ``Fn::If`` around a policy entry or inside an ``Action``
    list errors the whole file. That is correct — a dropped shape reads as "grants
    nothing" — but a dozen fixture errors do not say what to do, so this test exists
    to be the one failure that names the resource and the shape.

    It is also what keeps the refusal from being a tripwire somebody switches off:
    the live template must be readable, every time, and if a deliberate ``!If`` on a
    policy of this role is ever wanted, the reader has to learn to evaluate it.
    """
    try:
        policies = role_policies()
        statements = role_s3_statements()
        bucket_policies_reaching_documents()
        # The gate's own premise, and the one route the readers cannot see. Both
        # refuse an intrinsic rather than guessing at it, so both belong here.
        authenticated_role_logical_id()
        identity_pool_role_mappings()
        identity_pool_allows_unauthenticated()
        identity_pool_unauthenticated_roles()
        nested_stacks_given_the_role()
    except UnreadablePolicyShape as exc:
        raise AssertionError(
            f"a policy reaching {ROLE} is in a shape this gate cannot evaluate, so "
            "every assertion in this file is unsound rather than merely untested:\n"
            f"  {exc}\n"
            "An intrinsic that is dropped reads as 'grants nothing', which is how "
            "ten separate routes past this gate were found. Either restructure the "
            "template so the shape is literal, or teach browser_s3_policy to "
            "evaluate it — do not relax the refusal."
        ) from exc
    assert policies, f"{ROLE} declares no readable inline policy"
    assert isinstance(statements, list)


def test_the_role_grants_no_s3_at_all(s3_statements):
    """The claim this file exists to make, post-#1033.

    Every authenticated user assumes this role regardless of group and reaches
    whatever it grants with no resolver in the path, so there is no S3 object in this
    deployment whose read can be authorized here. The document read path is
    ``getFilePresignedUrl`` / ``getFilePresignedUrls``, which apply the group floor,
    the bucket allow-list and the per-document scope check.
    """
    named = sorted(_buckets_named(s3_statements))
    assert not s3_statements, (
        f"{ROLE} grants S3 again, on {named or 'a resource naming no bucket'} "
        f"(policies: {sorted({p for p, _ in s3_statements})}). Every signed-in user "
        "assumes this role irrespective of group — including one in no group — and "
        "reaches it with no resolver in the path, so nothing can apply a group "
        "check, a per-document check or a per-user scope check to it. That is the "
        "hole #1033 closed. Route the read through getFileContents / "
        "getFilePresignedUrl / getFilePresignedUrls instead: "
        + "; ".join(
            f"{b} {why}" for b, why in sorted(BROWSER_FORBIDDEN_BUCKETS.items())
        )
    )


def test_the_role_carries_exactly_the_expected_policies(policies):
    """The whole policy set, so a grant of any service has to be chosen.

    This is what keeps the removed KMS statement removed. It was not an S3 action, so
    none of the S3 assertions would have objected to it, and its only consumer was
    the client-presigned GET that no longer happens — the browser has no KMS client,
    this role never held ``s3:PutObject``, and ``SettingsParameter`` is a
    ``Type: String`` parameter that needs no key.

    A failure here is not automatically a defect: it means the browser's direct
    reach changed. Decide whether the thing it now calls has a per-user boundary
    (if so it belongs behind a resolver), write one member's worth of reason into
    :data:`EXPECTED_ROLE_POLICIES`, and update the note in ``docs/rbac.md`` in the
    same change.
    """
    assert set(policies) == set(EXPECTED_ROLE_POLICIES), (
        f"{ROLE}'s inline policies changed. Expected "
        f"{sorted(EXPECTED_ROLE_POLICIES)}, found {sorted(policies)}. Every "
        "authenticated user assumes this role regardless of group and calls what it "
        "allows with no resolver in the path, so each policy here needs its own "
        "reason: "
        + "; ".join(f"{n} — {why}" for n, why in sorted(EXPECTED_ROLE_POLICIES.items()))
    )


def test_no_standalone_policy_resource_attaches_to_this_role(s3_statements):
    """The second attachment route, asserted on the role rather than on the policy.

    An ``AWS::IAM::Policy`` or ``AWS::IAM::ManagedPolicy`` resource listing this role
    in its ``Roles`` grants it exactly as effectively as an inline entry, and nothing
    in the role resource says so — `template.yaml` uses that idiom twice already, to
    break circular dependencies, so it is the route a grant would most plausibly come
    back by without anyone noticing. ``role_s3_statements`` reads those policies too,
    so the S3 assertions above already cover their contents; this is the stronger
    claim, that there are none at all, because an attached policy granting something
    other than S3 is also a widening of what every signed-in user holds and no
    S3-shaped assertion would object to it.
    """
    attached = role_attached_policies()
    assert not attached, (
        f"{sorted(attached)} attach to {ROLE} as standalone policy resources. Every "
        "authenticated user assumes this role irrespective of group, so a grant "
        "arriving this way reaches all of them with no resolver in the path — and it "
        "is invisible to anyone reading the role resource. If one is genuinely needed "
        "(to break a circular dependency, as two others in this template do), decide "
        "it deliberately: add it to EXPECTED_ROLE_POLICIES with one member's worth of "
        "reason and relax this assertion to that set."
    )


def test_the_role_attaches_no_managed_policy():
    """The third route, and the one this gate cannot evaluate.

    An AWS-managed or customer-managed policy lives outside this template, so no
    assertion here can say what it grants — ``AmazonS3ReadOnlyAccess`` would read as
    an opaque ARN. The only sound claim is therefore that the list is empty, which is
    why it is asserted rather than inspected. A deliberate addition has to replace
    this test with something that reads the policy from the account, which is a
    different kind of check and should be a different decision.
    """
    arns = role_managed_policy_arns()
    assert not arns, (
        f"{ROLE} attaches managed policies {arns}, whose contents are not readable "
        "from this template — so neither this gate nor a reviewer of this repository "
        "can say what every signed-in user now holds. An AWS-managed read-only policy "
        "attached here would reinstate exactly what #1033 removed, and every other "
        "assertion in this file would stay green."
    )


def test_the_identity_pool_still_attaches_the_role_this_gate_reads():
    """The premise under every other assertion in this file, asserted.

    :data:`ROLE` is a literal, and every docstring here — and the registry entry for
    :data:`EXPECTED_ROLE_POLICIES` — rests on it being the role the browser assumes.
    Nothing checked that: a second role carrying the old S3 policy, with
    ``CognitoIdentityPoolSetRole`` repointed at it, leaves this file reading a role
    nobody assumes and every assertion green while the browser holds the grant again.

    Derived and then **compared** rather than simply followed. Following the
    attachment would make the gate quietly re-aim at whatever the pool points to,
    which is the right security answer and the wrong change-control one: repointing
    the browser's role is a decision, and it should fail here and be made
    deliberately.
    """
    attached = authenticated_role_logical_id()
    assert attached is not None, (
        "no AWS::Cognito::IdentityPoolRoleAttachment in template.yaml names an "
        "`authenticated` role this parser can read, so nothing in this file is known "
        "to be about the role the browser assumes"
    )
    assert attached == ROLE, (
        f"the Identity Pool attaches {attached!r} as its authenticated role, but this "
        f"gate reads {ROLE!r}. Every assertion here is therefore about a role nobody "
        "assumes. If the browser's role was deliberately replaced, point ROLE at the "
        "new one and re-read EXPECTED_ROLE_POLICIES — do not leave the two names "
        "disagreeing."
    )


def test_no_nested_stack_is_passed_this_role():
    """The one route the readers above cannot see, closed by refusing the input.

    Everything else in this file opens ``template.yaml``, so an ``AWS::IAM::Policy``
    inside a nested template with ``Roles: [!Ref BrowserRoleName]`` — the parent
    passing ``BrowserRoleName: !Ref CognitoAuthorizedRole`` — attaches to this role
    and is invisible here. This repository is built on nested stacks, so that is an
    ordinary shape rather than an exotic one.

    Asserted as "the role is never passed down" rather than "every template is read",
    because a nested stack cannot attach a policy to a role it was never told about,
    and one check closes the class where reading every template would bring its own
    blind spots. The cost is that a legitimate future reason to pass this role down
    fails here — which is the right place for that to be a decision.
    """
    given = nested_stacks_given_the_role()
    assert not given, (
        f"these nested stacks are passed {ROLE}: {given}. A nested template can "
        "attach a policy to it, and nothing in this file reads a nested template — "
        "so the grant would be live with every assertion here green. If there is a "
        "genuine reason to pass it down, extend this gate to read role-attaching "
        "resources in every template `scripts/discover_templates.sh cfn` finds, and "
        "do not simply allow the parameter."
    )


def test_the_identity_pool_hands_no_credentials_to_an_unauthenticated_caller():
    """The premise behind the word "authenticated" in every message in this file.

    Everything here says that whatever this role grants is held by every
    **authenticated** user, which assumes only an authenticated user obtains
    credentials at all. That assumption is one line of the template, and nothing read
    it until now.

    ⚠️ **Flipped, the outcome is strictly worse than the hole #1033 closed.** That
    chain needed a self-registered account and its ID token; an unauthenticated
    identity needs neither — ``cognito-identity get-id`` followed by
    ``get-credentials-for-identity``, with no logins at all, yields whatever the
    unauthenticated role grants. And it is an ordinary change to make for an ordinary
    reason — a guest preview, a public demo — by somebody with no cause to think they
    were touching document access.

    Both halves are asserted, because either alone is insufficient: the flag can be
    ``true`` with no unauthenticated role attached (harmless but not something to
    leave unexamined), and an ``unauthenticated`` role can be attached while the flag
    is ``false`` (inert today, live the moment the flag moves).
    """
    assert not identity_pool_allows_unauthenticated(), (
        "the Identity Pool sets AllowUnauthenticatedIdentities: true, so a caller "
        "with no login can obtain credentials from it. Every message in this file "
        "says 'every authenticated user' on the assumption that cannot happen. If "
        "guest access is genuinely wanted, whatever role it assumes has to go through "
        "the same statement reader this file applies to "
        f"{ROLE} — it must not simply be exempt from it."
    )
    unauthenticated = identity_pool_unauthenticated_roles()
    assert not unauthenticated, (
        f"the Identity Pool attaches an unauthenticated role: {unauthenticated}. "
        "That role is assumed with no login, so whatever it grants is reachable by "
        "anyone who can reach the pool — a strictly larger population than the "
        "self-registered account #1033 was measured against. It is inert only while "
        "AllowUnauthenticatedIdentities stays false, which is one line away."
    )


def test_the_identity_pool_declares_no_role_mappings():
    """Why "irrespective of group" is true, rather than a consequence assumed.

    A single ``authenticated`` role with no ``RoleMappings`` is the reason anything
    this role grants is held identically by every signed-in account, group or no
    group — the sentence every message in this file and in `docs/rbac.md` is built
    on. With role mappings in place the argument would be wrong rather than merely
    incomplete, so the premise is read instead of restated.

    Role mappings are also the shape of option B in #1033, which was rejected. If one
    appears, that decision was revisited and this file needs re-reading, not relaxing.
    """
    mappings = identity_pool_role_mappings()
    assert not mappings, (
        f"the Identity Pool declares RoleMappings for {sorted(mappings)}, so which "
        "role a signed-in user assumes now depends on their claims. Everything in "
        "this file, and the residual statements in docs/rbac.md and the threat model, "
        "assume one role for everyone — re-read them before changing this assertion."
    )


def test_no_document_bucket_policy_reaches_a_principal_that_could_be_this_role():
    """The other side of the authorization, which an identity-policy gate cannot see.

    For a caller in the same account, S3 requires an explicit ``Allow`` in **either**
    the identity policy or the bucket policy. So "the role grants no S3" does not by
    itself bound what the role may read, and this template already declares bucket
    policies, so the route is available rather than hypothetical.

    ⚠️ **Asked as "can the role be ruled out", not "is the role named".** The first
    version matched the literal ``CognitoAuthorizedRole`` in the principal, which left
    an **account-wide** principal — ``arn:…:iam::${AWS::AccountId}:root``, the
    idiomatic way a bucket policy over-grants, and broader than the grant #1033
    removed — untested against the S3 reader entirely. Only a named service principal
    rules the role out; everything else counts.
    """
    reaching = bucket_policies_reaching_documents()
    assert not reaching, (
        f"these bucket policies on {list(DOCUMENT_BUCKETS)} allow an S3 read to a "
        f"principal that could include {ROLE}: {reaching}. Every authenticated user "
        "assumes that role irrespective of group, so this authorizes the read with no "
        "resolver in the path exactly as an identity policy would — and every other "
        "assertion in this file would stay green, because the identity side really "
        "does grant nothing."
    )


@pytest.mark.parametrize(
    "bucket,why",
    sorted(BROWSER_FORBIDDEN_BUCKETS.items()),
    ids=sorted(BROWSER_FORBIDDEN_BUCKETS),
)
def test_a_named_bucket_is_not_browser_readable(s3_statements, bucket, why):
    """Per-bucket, so a failure names the content that just became reachable.

    Redundant with :func:`test_the_role_grants_no_s3_at_all` while the statement list
    is empty, and kept for the failure message: a reader who has just re-added one
    bucket gets told what is in it rather than being told the set is non-empty.
    """
    assert bucket not in _buckets_named(s3_statements), (
        f"{ROLE} grants S3 on {bucket}, which every authenticated user assumes "
        f"regardless of group and reaches with no resolver in the path. {bucket} {why} "
        "— so a grant here defeats that axis for every scoped user while the "
        "resolver-side check still passes. Route the read through getFileContents / "
        "getFilePresignedUrl / getFilePresignedUrls instead."
    )


def test_no_s3_grant_on_this_role_uses_a_wildcard_resource(s3_statements):
    """A wildcard names no bucket, so it evades the per-bucket assertion above.

    ``Resource: "*"`` (or ``arn:*:s3:::*``) contributes no logical id, so
    :func:`test_a_named_bucket_is_not_browser_readable` finds nothing to object to
    while the grant reaches every bucket in the account. It is the one shape that
    turns that assertion into a false negative, so it gets its own — and it stays
    even though the statement list is empty today, because the shape it covers is
    the one a re-added grant is least likely to be noticed in.
    """
    for policy, statement in s3_statements:
        for text in _resource_strings(statement):
            assert not _is_wildcard_resource(text), (
                f"{ROLE}.{policy} grants S3 on {text!r}, a wildcard. Every "
                "authenticated user assumes this role with no resolver in the path, "
                "so this reaches every bucket in the account — including the four "
                "whose contents are partitioned per user or per document. No S3 "
                "grant belongs on this role at all; see #1033."
            )


def test_no_browser_grant_allows_bucket_enumeration(s3_statements):
    """``s3:ListBucket`` is the enumeration primitive, so it keeps its own assertion.

    Object read needs a key; listing produces the keys. It was dropped from this role
    before the object grants were, so this assertion predates the rest of the file
    and is kept because it is the one that states *why* the two halves differ: a
    re-added `GetObject` is "read a document whose key you hold", a re-added
    `ListBucket` is "read every document", and a reader restoring one should be told
    which they restored.

    An action wildcard counts as a grant of it. ``s3:*`` and ``*`` reach
    ``ListBucket`` without naming it, so a statement spelled that way would satisfy a
    literal ``"s3:ListBucket" in actions`` test while granting exactly what this
    forbids.
    """
    for policy, statement in s3_statements:
        actions = {str(a) for a in _as_list(statement.get("Action"))}
        enumerating = {"s3:ListBucket", "s3:ListBucketVersions", "s3:*", "*"} & actions
        enumerable = _buckets_named([(policy, statement)]) if enumerating else set()
        assert not enumerating, (
            f"{ROLE}.{policy} grants {sorted(enumerating)}, which lets the browser "
            f"enumerate {sorted(enumerable) or 'every bucket it names'}. That hands "
            "any signed-in user the key space of each bucket named, with no resolver "
            "in the path to apply the caller's groups, scope or document ownership "
            "to either half. Route any genuine listing through a resolver."
        )


# ---------------------------------------------------------------------------
# Non-vacuity: the reader must still be able to SEE a grant.
#
# Everything above is an absence, and an absence is what a broken parser reports.
# These two drive the real collector and the real matcher over synthetic policies
# that do grant S3, so a reader that has stopped recognising a grant fails here
# rather than certifying the tree clean.
# ---------------------------------------------------------------------------


def test_the_statement_collector_still_recognises_a_grant():
    """Driven over a synthetic role, because the real one has nothing to find.

    The four spellings are the ways a grant could come back: under the old ``S3``
    policy name, under any other name, as a single statement rather than a list, and
    as an action wildcard that never writes ``s3:``. A collector that missed any of
    them would report ``[]`` for a role that grants the document buckets, and every
    assertion in this file would pass.
    """
    cases = {
        "old-policy-name": [
            {
                "PolicyName": "S3",
                "PolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["s3:GetObject"],
                            "Resource": "arn:aws:s3:::${OutputBucket}/*",
                        }
                    ]
                },
            }
        ],
        "another-policy-name": [
            {
                "PolicyName": "DocumentReads",
                "PolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "s3:GetObjectVersion",
                            "Resource": {"Fn::GetAtt": ["InputBucket", "Arn"]},
                        }
                    ]
                },
            }
        ],
        "scalar-statement": [
            {
                "PolicyName": "S3",
                "PolicyDocument": {
                    "Statement": {
                        "Effect": "Allow",
                        "Action": "s3:GetObject",
                        "Resource": {"Ref": "OutputBucket"},
                    }
                },
            }
        ],
        "action-wildcard": [
            {
                "PolicyName": "Everything",
                "PolicyDocument": {
                    "Statement": [{"Effect": "Allow", "Action": "*", "Resource": "*"}]
                },
            }
        ],
        # IAM action names are case-insensitive, so this is a live grant. A
        # `startswith("s3:")` test does not see it, which on a gate asserting "no S3
        # at all" is a false negative with nothing to report it.
        "mis-cased-action": [
            {
                "PolicyName": "S3",
                "PolicyDocument": {
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": ["S3:GetObject"],
                            "Resource": "arn:aws:s3:::${OutputBucket}/*",
                        }
                    ]
                },
            }
        ],
    }
    for name, policy_list in cases.items():
        found = _collect_s3_statements(policy_list)
        assert found, (
            f"browser_s3_policy.collect_s3_statements does not see the {name} "
            "spelling of an S3 grant, so a role carrying one would read as granting "
            "nothing and every assertion in this file would pass vacuously"
        )


def test_the_reader_refuses_a_shape_it_cannot_evaluate():
    """Every blind spot in this file has been an intrinsic the reader dropped.

    Ten routes past this gate were found across three review rounds, and each was the
    same defect one level up from the last: an ``Fn::If`` around a ``Policies`` entry,
    an ``Fn::If`` inside an ``Action`` list, a non-literal ``Effect``. Dropping an
    unreadable shape reads as "grants nothing", which is the direction that fails
    open, and adding a recognizer per route does not converge — each one closes the
    shape that was tried.

    What converges is refusing to read a shape at all, so these must raise rather
    than return. Two of them are valid CloudFormation that ``cfn-lint`` accepts, and
    the first is a form this template already uses on this very role's
    ``PermissionsBoundary``, so none of this is hypothetical.
    """
    policy_entry_if = [
        {
            "Fn::If": [
                "SomeCondition",
                {
                    "PolicyName": "S3",
                    "PolicyDocument": {
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": ["s3:GetObject"],
                                "Resource": "arn:aws:s3:::${OutputBucket}/*",
                            }
                        ]
                    },
                },
                {"Ref": "AWS::NoValue"},
            ]
        }
    ]
    with pytest.raises(UnreadablePolicyShape):
        _collect_s3_statements(policy_entry_if)

    action_if = [
        {
            "PolicyName": "ParameterStore",
            "PolicyDocument": {
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": {
                            "Fn::If": [
                                "SomeCondition",
                                ["ssm:GetParameter", "s3:GetObject"],
                                ["ssm:GetParameter"],
                            ]
                        },
                        "Resource": "*",
                    }
                ]
            },
        }
    ]
    with pytest.raises(UnreadablePolicyShape):
        _collect_s3_statements(action_if)

    for broken in (
        {"Effect": {"Fn::If": ["C", "Allow", "Deny"]}, "Action": ["s3:GetObject"]},
        {"Effect": "Allow", "NotAction": [{"Fn::If": ["C", "iam:*", "s3:*"]}]},
    ):
        with pytest.raises(UnreadablePolicyShape):
            statement_reaches_s3(broken)

    # And the live template must be readable, which is what makes the refusal a gate
    # rather than a tripwire somebody disables.
    assert role_policies()
    role_s3_statements()
    bucket_policies_reaching_documents()


def test_the_attachment_reader_refuses_a_value_naming_more_than_one_role():
    """The gate's premise is read, not guessed — driven over the shape that broke it.

    ``authenticated_role_logical_id`` took the FIRST regex match out of a flattened
    intrinsic, so ``!If [Cond, !GetAtt RoleA.Arn, !GetAtt RoleB.Arn]`` read as
    ``RoleA``: the comparison against ``ROLE`` passed while the browser assumed
    ``RoleB`` in the other branch. Driven here against the live template, because the
    function reads it directly — the positive case is that it still resolves to
    exactly one name, and the negative cases are asserted on the reader's rule rather
    than by rewriting the template.
    """
    assert authenticated_role_logical_id() == ROLE

    # The rule itself: exactly one literal reference, or refuse. Expressed over
    # `flatten_resource` so it is the same derivation the function uses.
    from browser_s3_policy import flatten_resource

    single = {"Fn::GetAtt": ["CognitoAuthorizedRole", "Arn"]}
    two_branches = {
        "Fn::If": [
            "SomeCondition",
            {"Fn::GetAtt": ["CognitoAuthorizedRole", "Arn"]},
            {"Fn::GetAtt": ["CognitoAuthorizedRoleAlt", "Arn"]},
        ]
    }

    def names(node):
        found = set()
        for text in flatten_resource(node):
            for match in re.finditer(
                r"([A-Za-z0-9]+)\.Arn|\$\{([A-Za-z0-9]+)\}", str(text)
            ):
                found.add(match.group(1) or match.group(2))
        return found

    assert names(single) == {ROLE}
    assert len(names(two_branches)) == 2, (
        "the flattener must surface BOTH branches of an Fn::If, or the reader cannot "
        "tell a two-role value from a one-role value and taking the first match is "
        "indistinguishable from reading it correctly"
    )


def test_a_principal_this_reader_cannot_rule_out_counts_as_the_role():
    """The inversion, driven over the shapes a bucket policy actually uses.

    The account-root principal is the one that matters: it includes every role in the
    account, it is how a bucket policy over-grants in practice, and a name-matching
    check never looked at it.
    """
    could_be = (
        ["arn:${AWS::Partition}:iam::${AWS::AccountId}:root"],
        ["*"],
        ["${AWS::AccountId}"],
        ["arn:aws:iam::123456789012:role/SomeOtherRole"],
        ["${CognitoAuthorizedRole.Arn}"],
        ["CognitoAuthorizedRole.Arn"],
    )
    for texts in could_be:
        assert principal_may_include_role(texts), texts

    ruled_out = (
        ["cloudfront.amazonaws.com"],
        ["logging.s3.amazonaws.com"],
        ["delivery.logs.amazonaws.com"],
    )
    for texts in ruled_out:
        assert not principal_may_include_role(texts), texts


def test_the_statement_reader_sees_a_grant_that_names_no_s3_action():
    """``NotAction`` and case, driven directly, because both read as an empty grant.

    Each of these is an ``Allow`` that can authorize ``s3:GetObject``, and each
    defeated a plain ``"s3:" in Action`` reader. The negative cases matter as much:
    a ``NotAction`` that blanket-excludes S3 does not reach it, and neither does a
    ``Deny``, so a predicate that answered "reaches S3" to everything would make the
    policy-set assertion fire on correct statements and get itself weakened.
    """
    reaching = {
        "lower": {"Effect": "Allow", "Action": ["s3:GetObject"]},
        "mis-cased": {"Effect": "Allow", "Action": ["S3:GetObject"]},
        "service-wildcard": {"Effect": "Allow", "Action": "s3:*"},
        "action-wildcard": {"Effect": "Allow", "Action": "*"},
        "not-action-elsewhere": {"Effect": "Allow", "NotAction": ["iam:*"]},
        "not-action-narrow-s3": {"Effect": "Allow", "NotAction": ["s3:PutObject"]},
        "effect-defaulted": {"Action": ["s3:GetObject"]},
    }
    for name, statement in reaching.items():
        assert statement_reaches_s3(statement), (
            f"browser_s3_policy.statement_reaches_s3 does not see the {name} shape as "
            "reaching S3, so a grant written that way is invisible to every "
            "assertion in this file"
        )

    not_reaching = {
        "other-service": {"Effect": "Allow", "Action": ["ssm:GetParameter"]},
        "not-action-blankets-s3": {"Effect": "Allow", "NotAction": ["s3:*"]},
        "not-action-blankets-all": {"Effect": "Allow", "NotAction": ["*"]},
        "deny": {"Effect": "Deny", "Action": ["s3:GetObject"]},
        "deny-not-action": {"Effect": "Deny", "NotAction": ["iam:*"]},
    }
    for name, statement in not_reaching.items():
        assert not statement_reaches_s3(statement), name


def test_every_role_attaching_resource_type_names_its_role_property():
    """The type-to-property map is what makes the attached-policy sweep complete.

    ``AWS::IAM::RolePolicy`` is the member that matters: it attaches an inline policy
    to an existing role through the singular ``RoleName``, so a sweep that reads only
    ``Roles`` finds nothing while the policy is live. It is also the resource type a
    reader who has just closed the ``AWS::IAM::Policy`` route would reach for next.
    """
    assert set(ROLE_ATTACHING_TYPES) == {
        "AWS::IAM::Policy",
        "AWS::IAM::ManagedPolicy",
        "AWS::IAM::RolePolicy",
    }, sorted(ROLE_ATTACHING_TYPES)
    assert ROLE_ATTACHING_TYPES["AWS::IAM::RolePolicy"] == ("RoleName",)


def test_the_attached_policy_route_recognises_every_roles_spelling():
    """Driven synthetically, because the live template has no such policy.

    ``role_attached_policies`` returns ``{}`` today, and so would a recognizer that
    matched nothing — which is the same false negative as a broken statement
    collector, one level up. Each spelling below is a way ``template.yaml`` could name
    this role in a standalone policy's ``Roles``; two of them are the idioms it
    already uses for the two policies that exist.
    """
    spellings = {
        "ref": {"Roles": [{"Ref": ROLE}]},
        "fn-ref": {"Roles": [{"Fn::Ref": ROLE}]},
        "literal": {"Roles": [ROLE]},
        "scalar-not-a-list": {"Roles": {"Ref": ROLE}},
        "sub": {"Roles": [{"Fn::Sub": "${" + ROLE + "}"}]},
        "among-others": {"Roles": [{"Ref": "SomeOtherRole"}, {"Ref": ROLE}]},
        # AWS::IAM::RolePolicy's singular property. Same idiom, one name away.
        "role-name-ref": {"RoleName": {"Ref": ROLE}},
        "role-name-literal": {"RoleName": ROLE},
    }
    for name, properties in spellings.items():
        assert policy_names_role(properties), (
            f"browser_s3_policy.policy_names_role does not recognise the {name} "
            f"spelling of a Roles reference to {ROLE}, so a standalone "
            "AWS::IAM::Policy attached that way would be invisible to this file"
        )
    for name, properties in {
        "no-roles": {},
        "another-role": {"Roles": [{"Ref": "SomeOtherRole"}]},
        "empty": {"Roles": []},
    }.items():
        assert not policy_names_role(properties), name


def test_the_resource_matcher_reads_every_cloudformation_idiom():
    """The matcher's own coverage, asserted rather than assumed.

    Each idiom below is a way ``template.yaml`` could name a bucket tomorrow. An
    idiom the flattener drops reads as "no bucket named", which is the direction that
    fails open — and two of these (the ``Fn::GetAtt`` list form and ``Ref``) were
    dropped by the first version of this gate, so this is not a hypothetical class.
    """
    cases = {
        "sub-scalar": {
            "Fn::Sub": "arn:${AWS::Partition}:s3:::${ConfigurationBucket}/*"
        },
        "sub-list": {"Fn::Sub": ["arn:aws:s3:::${ConfigurationBucket}/*", {"X": "y"}]},
        "getatt-scalar": {"Fn::GetAtt": "ConfigurationBucket.Arn"},
        "getatt-list": {"Fn::GetAtt": ["ConfigurationBucket", "Arn"]},
        "ref": {"Ref": "ConfigurationBucket"},
        "fn-ref": {"Fn::Ref": "ConfigurationBucket"},
        "join-over-ref": {
            "Fn::Join": ["", ["arn:aws:s3:::", {"Ref": "ConfigurationBucket"}, "/*"]]
        },
        "join-over-getatt-list": {
            "Fn::Join": ["", [{"Fn::GetAtt": ["ConfigurationBucket", "Arn"]}, "/*"]]
        },
    }
    for name, resource in cases.items():
        found = _buckets_named([("Probe", {"Resource": resource})])
        assert found == {"ConfigurationBucket"}, (
            f"the {name} idiom is not read by browser_s3_policy.flatten_resource / "
            f"buckets_named "
            f"(got {found or 'nothing'}), so a bucket named that way would be "
            "invisible to every assertion in this file"
        )

    for wildcard in ("*", "arn:aws:s3:::*", "arn:${AWS::Partition}:s3:::*/*"):
        assert _is_wildcard_resource(wildcard), wildcard
    for specific in (
        "arn:${AWS::Partition}:s3:::${InputBucket}/*",
        "arn:aws:s3:::my-bucket/*",
    ):
        assert not _is_wildcard_resource(specific), specific
