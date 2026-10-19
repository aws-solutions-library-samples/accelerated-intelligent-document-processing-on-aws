# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Per-DOCUMENT authorization for Input- and Output-bucket object reads.

``s3_targets`` bounds *which bucket* a caller-supplied ``s3Uri`` may name and
``key_scope`` bounds *which key* within the two buckets that are partitioned by a
per-user axis. Neither one answers anything about a document:
``key_scope.scope_subject`` returns a scope subject only for the Configuration and
Test Set buckets, so ``key_scope.assert_key_in_scope`` is a **no-op on every
Input- and Output-bucket key**. Moving a document read behind this resolver
therefore buys the field-level group floor and the bucket allow-list and nothing
per-document — a caller holding any group could still name any other document's
page image. This module is the missing half.

**The axis is the one the product actually records.** There is no document-owner
attribute anywhere in this deployment, so "is this document yours" cannot be
asked. What *is* recorded is the configuration profile a document was processed
under (``Document.ConfigVersion`` on the tracking table) and, per user, the
profiles they may see (``allowedConfigVersions`` on the UsersTable). That pairing
is already the document boundary for ``listDocuments``, ``getDocumentCount``,
``reprocessDocument`` and document chat; this module applies the same pairing to
the bytes, using ``config_scope.scope_allows`` and
``config_scope.resolve_allowed_config_versions`` unchanged so a check that admits
here cannot deny there.

⚠️ **What that does and does not close, stated so nobody over-reads a green
gate.** ``resolve_allowed_config_versions`` returns ``None`` — deliberately
unrestricted — for a caller whose row sets no scope and for a caller no UsersTable
row matches at all, which includes a self-registered account. Scoping is opt-in
per user and must stay that way, so for an **unscoped** caller this module is a
no-op by design and the cross-document read is bounded only by the group floor.
For a **scoped** caller it is a real refusal: a page image belonging to a document
stamped with a profile outside their ``allowedConfigVersions`` is denied before S3
is touched. That is exactly the reach ``listDocuments`` has, and it is the whole
of the per-document boundary this product can express.

Three properties, each with its own test:

* **Fail closed on both halves.** A scope that cannot be *evaluated*
  (``ScopeLookupError``: no UsersTable wired, no usable claim, a failed DynamoDB
  read) denies. A document that cannot be *identified* — no tracking row for any
  prefix of the key — denies. A document carrying no ``ConfigVersion`` denies a
  scoped caller, because an unstamped document cannot be proven in scope. The
  one "absence means allow" branch is the unrestricted caller above, and that is
  an answer from the table rather than a failure to get one.
* **A refusal discloses nothing.** Every denial raises the same refusal text — the
  identical string ``key_scope`` uses — naming neither the bucket, the key, the
  document, the profile, nor whether any of them exist. The object is never read, so S3 is never asked a question whose answer
  could be timed. This matters more for the batch field than for the single one:
  per-entry refusals would turn one call into an N-key authorization oracle, so
  the batch handler refuses the **whole** request instead (see ``index.py``).
* **Bounded work.** The owning document is found by walking the key's ``/``
  boundaries from longest to shortest, so the candidates per key are bounded by its
  segment count; every answer is memoised per invocation, and a document's N page
  keys share every ancestor but their two deepest, so the whole request's candidates
  collapse to roughly ``2N`` and resolve 100 per ``BatchGetItem`` — six round trips
  for a 269-page view rather than 540. A per-invocation budget on those **round
  trips** bounds the amplification an uncapped list of caller-supplied keys would
  otherwise allow. ⚠️ The budget's refusal is **not** reported as "your request was
  too broad": round-trip count is a function of which candidate documents exist, so
  a distinguishable status would be an existence oracle, and
  ``index._budget_refusal`` converts it into the ordinary opaque denial.

Which control refused, and why the answer is not the whole module
-----------------------------------------------------------------
A refusal here is a 403 with ``errorType: "Unauthorized"``, which is the shape the
field-level group floor uses too, so the dispatcher labels every denial with
``errors[0].authzControl`` to say *which control* answered. A per-object control
declares itself by raising an exception named ``ResourceAccessDenied``, recognised
by class **name** because the object does not cross the ``lambda:Invoke`` boundary;
anything else authorization-shaped is labelled ``resolver``, meaning "a resolver
refused and did not say". ``resolver`` is read strictly downstream — a per-object
control that does not declare itself costs a visible failure in the live RBAC
matrix rather than a silent pass — so the declaration is not cosmetic.

The split is **per branch**, and the line is whether the refusal is a decision
about the object the caller named:

* ``ResourceAccessDenied`` -> ``resource``: the three refusals in
  :func:`assert_document_in_scope` (the key names no object, no tracked document
  owns it, the owner's profile is outside the caller's scope) and the lookup-budget
  overrun that ``index._budget_refusal`` converts.
* bare ``PermissionError`` -> ``resolver``: a scope that could not be *evaluated*
  (:func:`resolve_caller_scope`), a tracking-table read that stayed throttled, and
  an unwired tracking table. None of those is about the object; the first two are
  about the principal and the deployment, and the third is a template fault — the
  same reading ``s3_targets`` gives its empty-allow-list branch.

⚠️ **The uniformity inside the first group is the load-bearing part.** Labelling
only the profile mismatch would separate "this document exists and is outside your
scope" from "no such document", which is an existence oracle at the API — exactly
what the single refusal string above exists to prevent. The budget overrun is in
that group for the same reason: round-trip count is a function of which candidates
exist, so it must be indistinguishable from an ordinary denial in the label as well
as in the status. The ``resolver`` branches leak nothing by contrast, because none
of them varies with the key: they answer the same way for every object a given
caller names.

Which document owns an Output-bucket key
---------------------------------------
An Output-bucket artifact is written under ``<ObjectKey>/`` — ``pages/<n>/image.*``,
``sections/<n>/result.json``, ``summary/``, ``evaluation/``, ``runs/<id>/`` and so on
— and ``ObjectKey`` is an uploaded S3 key, which may itself contain ``/``. So the
document has to be derived from the key, and the derivation must be sound for **every**
artifact path, including one nobody has written yet.

The rule is therefore one rule, not a fast path plus a fallback: **the candidates are
the key and all of its ancestors, strictly longest first, and the owner is the first
one the tracking table has a row for.** The longest existing prefix is the owner,
because artifacts live under their document. Where uploads nest — ``foo`` and
``foo/bar.pdf`` both documents, a case ``idp_common.document_versions`` calls out —
``foo/bar.pdf/pages/1/image.jpg`` is ``foo/bar.pdf``'s page, since ``foo``'s own pages
are at ``foo/pages/1/image.jpg``.

⚠️ **An earlier form of this took a shortcut that was wrong, and the shape of the
mistake is the thing this design exists to avoid.** It proposed the ancestors whose
next segment named a *known* artifact store first, so that a document's N page keys
shared one candidate and a batch cost one lookup. For every artifact store it knew
about, that was both faster and correct. For one it did not, it put a **shorter**
ancestor ahead of the true owner: for ``a/pages/b.pdf/foo/x.json`` it proposed ``a``
— because ``a`` is followed by ``pages`` — before ``a/pages/b.pdf``, so with documents
at both, a caller scoped to ``a``'s profile was authorized to read
``a/pages/b.pdf``'s artifact. The list being incomplete was documented as costing
"latency and nothing else", and it did not: it cost a cross-document read, in exactly
the direction this module must not fail. A derivation whose soundness depends on a
list of names staying complete is not sound.

What pays for the strict ordering is the **read**, not the ordering. One
``BatchGetItem`` resolves up to 100 candidates per round trip, so a 269-page document
view — about 540 distinct candidates once the ancestors are deduplicated across every
page key — costs six round trips rather than 540, and the batch field primes the whole
request's candidates before any key is judged. Reads are strongly consistent: a
document row written moments earlier must be visible, because not seeing it denies.

A key no candidate resolves to is **refused**, not exempted: it belongs to no document
this deployment tracks, so there is nothing to prove it is in scope.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Mapping, Optional, Sequence

import config_scope
import key_scope
from s3_targets import ResourceAccessDenied

logger = logging.getLogger(__name__)

# The one refusal message, shared **verbatim** with ``key_scope._REFUSAL``. Not
# imported from there, and not worded differently either: a caller must not be able
# to tell a per-document refusal from a per-profile one from a per-test-set one, and
# a second string is how that distinction leaks back in. ``test_document_scope.py``
# asserts the two literals are equal.
#
# The ``Unauthorized`` prefix is load-bearing and not decoration: it is one of the
# two things that map a resolver's exception to a 403 rather than a 500, the other
# being the class name. Keep it on every refusal, whichever class carries it.
REFUSAL = "Unauthorized: you are not permitted to read that object."

# Groups that are never configuration-profile scoped. Mirrors ``key_scope`` and the
# configuration resolver, which resolve no scope at all for an Admin. The three must
# agree: a caller one path serves and another refuses is a divergence between two
# enforcements of one rule.
UNSCOPED_GROUPS = ("Admin",)

# The tracking-table item that records a document, and the attributes its
# configuration profile may be under. ``ConfigurationVersion`` is the older spelling
# and is read for the same reason the chat processor reads it: rows written before
# the rename still carry it, and treating one as unstamped would deny a scoped caller
# a document that is in their scope.
_DOCUMENT_PK_PREFIX = "doc#"
_DOCUMENT_SK = "none"
_CONFIG_VERSION_ATTRIBUTES = ("ConfigVersion", "ConfigurationVersion")

# How many tracking-table ROUND TRIPS one invocation may make.
#
# A candidate is a prefix of a key the caller supplied, so an uncapped derivation is a
# request amplifier: `MAX_BATCH_OBJECTS` keys of arbitrary depth produce arbitrarily
# many candidates. What is capped is the number of `BatchGetItem` calls rather than the
# number of candidates, because that is what costs time — each call resolves up to
# `_CANDIDATES_PER_BATCH` of them in one round trip.
#
# 64 round trips is up to 6,400 candidates, far more than any real request needs: the
# 269-page document view the benchmarking pages describe is six. A request that needs
# more than this is not a document view, and it is refused rather than served slowly.
# Only a SCOPED caller reaches the table at all.
#
# ⚠️ The budget counts **every** `batch_get_item` call, retries included. Counting only
# the distinct batches made the stated bound 4x optimistic — `_UNPROCESSED_RETRIES`
# allows four attempts per batch, so 64 batches could issue 256 calls while the 20 s
# reasoning rested on 64.
MAX_DOCUMENT_LOOKUP_BATCHES = 64

#: DynamoDB's own per-`BatchGetItem` limit.
_CANDIDATES_PER_BATCH = 100

#: How many times an incomplete `BatchGetItem` is retried before the request is
#: refused. DynamoDB returns `UnprocessedKeys` under throttling, and a key left
#: unresolved reads as "no such document", which denies — so retrying is what keeps a
#: throttle from presenting as an authorization failure. Bounded, because the
#: dispatcher abandons this function at 20 s and an unbounded retry would spend all of
#: it.
#
#: ⚠️ With **backoff**, which is what makes the retry do the thing above. Four
#: immediate retries against a throttled partition are four throttled calls, so the
#: first version of this retried in under 10 microseconds total and would almost
#: certainly have failed anyway. The waits are 50/100/200 ms, 350 ms in all, which is
#: well inside the dispatcher's 20 s even at the full round-trip budget.
_UNPROCESSED_RETRIES = 3
_UNPROCESSED_BACKOFF_SECONDS = 0.05


class DocumentLookupBudgetExceeded(Exception):
    """The invocation asked about more documents than one call may resolve.

    ⚠️ **Not reported to the caller as a 400, and the reason is an oracle.** The
    obvious reading is that the caller asked for too much and should be told so. But
    how many round trips a request costs is a function of **which candidates exist** —
    the walk stops at the first row it finds — so a caller who pads a batch to the
    boundary can read the status code as one bit about whether an arbitrary object key
    is a tracked document, for documents entirely outside their scope. That is
    precisely the distinction the single refusal message is worded to avoid, and it
    needs no timing analysis because the two answers are different status classes.

    ``index._budget_refusal`` therefore converts this into the same opaque
    ``ResourceAccessDenied`` the per-object branches of
    :func:`assert_document_in_scope` raise, carrying the same message. The type
    still exists so the conversion happens in one place and so the log line can say
    what actually happened; the caller learns nothing it could not learn from a
    refusal.

    ⚠️ The **class** matters as much as the status, for the same reason. It reaches
    the client as ``authzControl``, so converting this to the unattributed
    ``PermissionError`` while the ordinary scope denial declared itself would put
    the distinction the status class no longer carries back into the label.
    """


def is_document_bucket(
    bucket: str, *, input_bucket: str = "", output_bucket: str = ""
) -> bool:
    """Whether reads in ``bucket`` are scoped to a document.

    Bucket names are passed in rather than read from the environment so the caller
    resolves them once and so a test can state them explicitly. An **empty** name
    matches nothing: an unset ``INPUT_BUCKET`` must not make every bucket the input
    bucket, which ``bucket == os.environ.get(...)`` would do for a request naming a
    bucket the allow-list somehow admitted with no name configured.
    """
    return bool(
        (input_bucket and bucket == input_bucket)
        or (output_bucket and bucket == output_bucket)
    )


def document_key_candidates(
    bucket: str, key: str, *, input_bucket: str = "", output_bucket: str = ""
) -> Optional[list]:
    """Object keys that could own ``key``, longest first, or ``None``.

    ``None`` means the bucket is not document-scoped and this module has nothing to
    say about the key. An empty list is never returned: a document-bucket key always
    yields at least itself.

    For the **Input** bucket the key *is* the document's ``ObjectKey`` — that is what
    the uploader writes and what the tracking row is keyed on — so the only candidate
    is the key itself. For the **Output** bucket it is the key and every ancestor,
    **strictly longest first**: the owner is the longest one that exists, and nothing
    about the artifact path is assumed. See the module docstring for why the ordering
    is the whole rule and for what went wrong when it was not strict.

    Raises ``ValueError`` — a 400, not a refusal — for a key that is not in canonical
    form, via ``key_scope.assert_canonical_key`` (the same function, not a second
    copy of the rule). That check is load-bearing here rather than tidiness: the
    candidate walk splits on ``/``, so ``a//b.pdf/pages/1/image.jpg`` and
    ``./a/b.pdf/pages/1/image.jpg`` would otherwise derive candidate lists that name
    a *different* document from the one a normalising intermediary would eventually
    fetch. ``getFilePresignedUrl`` hands the signed key to a client this deployment
    does not control, which is what makes the difference reachable.
    """
    if not is_document_bucket(
        bucket, input_bucket=input_bucket, output_bucket=output_bucket
    ):
        return None

    key_scope.assert_canonical_key(key)
    # `assert_canonical_key` already refuses an empty segment, so a trailing `/`,
    # a leading `/` and an internal `//` have raised by now. The normalisation
    # below is kept anyway: it costs nothing and it means a future caller that
    # reaches this function without the canonical check cannot derive a candidate
    # list naming a shorter document than the key does.
    segments = [segment for segment in key.split("/") if segment]
    if not segments:
        # No object key at all. Not a document, and not "unscoped" either: the empty
        # candidate list is refused by `assert_document_in_scope` rather than passed.
        return []

    if input_bucket and bucket == input_bucket:
        return ["/".join(segments)]

    return ["/".join(segments[:n]) for n in range(len(segments), 0, -1)]


def _config_version(item: Mapping[str, Any]) -> Optional[str]:
    for attribute in _CONFIG_VERSION_ATTRIBUTES:
        value = item.get(attribute)
        if value:
            return str(value)
    return None


class DocumentResolver:
    """Finds the document that owns an object key, memoised for one invocation.

    One instance per request, not per container: the memo holds the tracking table's
    answers, and a container-lifetime cache of them would serve a document's old
    ``ConfigVersion`` after a reprocess changed it — which, on a restriction, is the
    direction that fails open. The caller's *scope* is cached per container instead
    (that is ``config_scope``'s own cache, keyed on the caller), because a scope
    change is an administrative act with its own 60-second bound.

    Resolution is by ``BatchGetItem``, which is what makes a strictly depth-ordered
    candidate walk affordable: :meth:`prime` takes every key the request names, unions
    their candidates, and resolves them 100 per round trip, so a 269-page document view
    is six round trips rather than 540. :meth:`owning_document` then reads the memo.
    Priming is an optimisation and never a correctness requirement — a caller that
    skips it gets the same answers, one round trip per key.
    """

    def __init__(
        self,
        dynamodb: Any,
        table_name: str,
        *,
        budget: int = MAX_DOCUMENT_LOOKUP_BATCHES,
    ):
        self._dynamodb = dynamodb
        self._table_name = table_name
        self._budget = budget
        self._seen: dict = {}
        self._batches = 0

    @property
    def batches(self) -> int:
        """Tracking-table round trips this instance has made."""
        return self._batches

    @property
    def lookups(self) -> int:
        """Distinct candidate keys this instance has resolved."""
        return len(self._seen)

    def prime(self, candidate_lists: Sequence[Sequence[str]]) -> None:
        """Resolve every candidate in ``candidate_lists`` that is not already known.

        The union across the whole request, so the ancestors a document's page keys
        share are fetched once. Order within a batch does not matter — the ordering
        that decides the owner is applied in :meth:`owning_document`, against the memo.
        """
        pending = []
        queued = set()
        for candidates in candidate_lists:
            for candidate in candidates:
                if candidate not in self._seen and candidate not in queued:
                    queued.add(candidate)
                    pending.append(candidate)
        for start in range(0, len(pending), _CANDIDATES_PER_BATCH):
            self._fetch(pending[start : start + _CANDIDATES_PER_BATCH])

    def _fetch(self, keys: Sequence[str]) -> None:
        """One ``BatchGetItem`` round trip (plus bounded retries), into the memo."""
        if not keys:
            return
        request = {
            self._table_name: {
                "Keys": [
                    {"PK": f"{_DOCUMENT_PK_PREFIX}{key}", "SK": _DOCUMENT_SK}
                    for key in keys
                ],
                # Strongly consistent on purpose. An eventually-consistent read that
                # has not caught up reports no row, and no row DENIES — so a document
                # written moments ago would be refused to its own uploader. The extra
                # read capacity is the cheaper half of that trade.
                "ConsistentRead": True,
                # Only what the check reads. `PK` is needed to join the response back
                # to the key it answers, since BatchGetItem returns items unordered
                # and omits the ones that do not exist.
                "ProjectionExpression": "PK, ConfigVersion, ConfigurationVersion",
            }
        }
        found: dict = {}
        for attempt in range(_UNPROCESSED_RETRIES + 1):
            # Counted here rather than once per `_fetch`, so a retry spends budget.
            # Counting batches instead made the documented bound four times
            # optimistic, and the 20 s timeout reasoning rested on the documented one.
            if self._batches >= self._budget:
                raise DocumentLookupBudgetExceeded(
                    f"this request would need more than {self._budget} tracking-table "
                    "round trips"
                )
            self._batches += 1
            if attempt:
                # Backoff, because four immediate retries against a throttled
                # partition are four throttled calls — which would make this loop
                # look like a mitigation without being one.
                time.sleep(_UNPROCESSED_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            response = self._dynamodb.batch_get_item(RequestItems=request)
            for item in (response.get("Responses") or {}).get(self._table_name, []):
                pk = str(item.get("PK", ""))
                if pk.startswith(_DOCUMENT_PK_PREFIX):
                    found[pk[len(_DOCUMENT_PK_PREFIX) :]] = item
            unprocessed = (response.get("UnprocessedKeys") or {}).get(self._table_name)
            if not unprocessed:
                break
            if attempt == _UNPROCESSED_RETRIES:
                # Fails CLOSED, loudly. An unresolved candidate reads as "no such
                # document", which denies — so the alternative to raising here is a
                # throttle presenting as an authorization failure, with nothing to
                # say which it was.
                #
                # Deliberately the UNATTRIBUTED class, unlike the three refusals in
                # `assert_document_in_scope`: this is a fact about the table, not a
                # decision about the object, so `authzControl: "resolver"` is the
                # true answer and is the row an operator should be looking at. It
                # discloses nothing either, because it does not depend on the key.
                logger.error(
                    "Refusing a document-bucket read: the tracking table left %d "
                    "candidate(s) unprocessed after %d attempts, so the owning "
                    "document could not be determined.",
                    len(unprocessed.get("Keys", [])),
                    _UNPROCESSED_RETRIES + 1,
                )
                raise PermissionError(REFUSAL)
            request = {self._table_name: unprocessed}

        # Every key asked for is recorded, present or absent, so a second request for
        # the same key costs nothing and an absent one is remembered as absent rather
        # than re-fetched.
        for key in keys:
            self._seen[key] = found.get(key)

    def owning_document(self, candidates: Sequence[str]) -> Optional[tuple]:
        """``(object_key, config_version)`` for the first candidate that exists.

        ``None`` when no candidate names a document this deployment tracks. The
        candidates are ordered longest-first by :func:`document_key_candidates`, and
        that ordering is the rule: the first hit is the owner.
        """
        unknown = [c for c in candidates if c not in self._seen]
        if unknown:
            self.prime([unknown])
        for candidate in candidates:
            item = self._seen.get(candidate)
            if item is not None:
                return candidate, _config_version(item)
        return None


def resolve_caller_scope(
    event: Optional[Mapping[str, Any]],
    *,
    users_table_name: str,
    dynamodb_factory: Any,
    cache: Optional[dict] = None,
    cache_ttl: float = config_scope.SCOPE_CACHE_TTL_SECONDS,
) -> Optional[list]:
    """The caller's ``allowedConfigVersions``, or ``None`` for unrestricted.

    Raises ``PermissionError`` with the shared refusal text when the caller cannot
    be resolved at all or when the lookup fails — the AUTH.T07 fail-closed shape.
    An ``Admin`` is unrestricted without a lookup, matching ``key_scope`` and the
    configuration resolver.

    ``dynamodb_factory`` is a zero-argument callable, **not** a resource, and the
    difference is load-bearing rather than stylistic: three of the branches below
    return before any table is read — an absent identity, absent claims, and an
    ``Admin`` — and an eagerly-evaluated ``boto3.resource("dynamodb")`` argument is
    constructed for all of them. That costs a client build on every Admin request,
    and in an environment with no region configured it *raises*, which turned a
    request this module was supposed to wave through into a 500. The hermetic test
    wrapper strips every region variable, so that is exactly where it showed up.

    The identity requirement is the same one ``key_scope._claims`` states and for
    the same reason: this function's only invoker is ``http_api_dispatcher``, which
    always builds the identity from the authorizer's verified claims, so there is no
    invoke this branch refuses that anything makes today — and reinstating "absent
    means trusted" would make every check below removable by dropping a key from the
    event.
    """
    identity = (event or {}).get("identity")
    if not isinstance(identity, Mapping):
        logger.warning(
            "Refusing a document-bucket object read: the event carries no verified "
            "identity, so no scope can be evaluated for it."
        )
        raise PermissionError(REFUSAL)
    claims = identity.get("claims")
    if not isinstance(claims, Mapping):
        logger.warning(
            "Refusing a document-bucket object read: the event's identity carries "
            "no claims, so no scope can be evaluated for it."
        )
        raise PermissionError(REFUSAL)

    groups = claims.get("cognito:groups") or []
    if isinstance(groups, str):
        groups = [groups]
    if any(group in UNSCOPED_GROUPS for group in groups):
        return None

    try:
        return config_scope.resolve_allowed_config_versions(
            config_scope.caller_email_from_claims(claims),
            caller_sub=config_scope.caller_sub_from_claims(claims),
            users_table_name=users_table_name,
            dynamodb=dynamodb_factory(),
            cache=cache,
            cache_ttl=cache_ttl,
        )
    except config_scope.ScopeLookupError as exc:
        logger.error(
            "Refusing a document-bucket object read: the caller's "
            "allowedConfigVersions could not be resolved: %s",
            exc,
        )
        raise PermissionError(REFUSAL) from exc


def assert_document_in_scope(
    bucket: str,
    key: str,
    allowed_versions: Optional[Sequence[str]],
    resolver: Optional[DocumentResolver],
    *,
    input_bucket: str = "",
    output_bucket: str = "",
) -> None:
    """Refuse the read unless the caller's profile scope covers this key's document.

    A no-op when ``bucket`` is not a document bucket, and a no-op when
    ``allowed_versions`` is ``None`` (an unrestricted caller — see the module
    docstring for why that is an answer rather than a gap). Otherwise raises
    ``ResourceAccessDenied`` with a message that discloses nothing about the object.

    Every refusal in this function raises that one class, and uniformly: it is the
    function that decides about the object the caller named, and a per-branch label
    here would be the existence oracle the shared message avoids. The fail-closed
    refusals *outside* it — an unevaluable scope, a throttled table — keep the
    unattributed ``PermissionError``; see the module docstring.

    ``resolver`` may be ``None`` only when no lookup can be needed (not a document
    bucket, or an unrestricted caller); passing ``None`` where a lookup *is* needed
    raises rather than skipping the check, so a caller that forgot to build one gets
    a 500 instead of an unchecked read.
    """
    candidates = document_key_candidates(
        bucket, key, input_bucket=input_bucket, output_bucket=output_bucket
    )
    if candidates is None:
        return
    if allowed_versions is None:
        return

    if resolver is None:  # pragma: no cover - defensive; a caller-side mistake
        raise RuntimeError(
            "assert_document_in_scope needs a DocumentResolver for a scoped caller"
        )

    if not candidates:
        logger.warning(
            "Refusing a document-bucket read: the key names no object, so no "
            "document can be identified for it."
        )
        raise ResourceAccessDenied(REFUSAL)

    owner = resolver.owning_document(candidates)
    if owner is None:
        # Fails CLOSED. A key under no tracked document cannot be proven to be in
        # the caller's scope, and reading it as unscoped would exempt exactly the
        # keys this check exists to bound.
        logger.warning(
            "Refusing a document-bucket read: no tracked document owns this key, "
            "so its configuration profile cannot be determined."
        )
        raise ResourceAccessDenied(REFUSAL)

    _object_key, config_version = owner
    if not config_scope.scope_allows(allowed_versions, config_version):
        # Covers both the mismatch and the unstamped document: `scope_allows`
        # denies a `None` name against a set scope, which is the rule the chat
        # processor and both document-list resolvers already apply.
        logger.warning(
            "Refusing a document-bucket read: the owning document's configuration "
            "profile is outside the caller's allowedConfigVersions."
        )
        raise ResourceAccessDenied(REFUSAL)


def resolve_bucket_names(env: Optional[Mapping[str, str]] = None) -> tuple:
    """``(input_bucket, output_bucket)`` from the environment the template wires.

    Stripped, so a variable set to whitespace is not a wired bucket — the same rule
    ``s3_targets.resolve_allowed_buckets`` applies, and for the same reason: a
    whitespace value that compared equal to nothing while reading as *configured*
    would make this module silently inert on both buckets.
    """
    source = os.environ if env is None else env
    return (
        (source.get("INPUT_BUCKET") or "").strip(),
        (source.get("OUTPUT_BUCKET") or "").strip(),
    )
