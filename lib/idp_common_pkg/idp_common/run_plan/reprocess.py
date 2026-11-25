# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Running a document again, from the beginning or from a later step.

``ReprocessService`` is the one implementation behind Reprocess, send-back and
restarting a failed run. For each document it checks that the run can start
(``eligibility.check``), decides the configuration the run is pinned to,
withdraws a live review, clears what the run will produce again and queues the
document with a run plan.

The caller authorises the request. The service takes the caller's
``allowed_versions`` and refuses a document or a profile outside them, so a
caller with a configuration scope cannot reach outside it through this code
either.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Literal,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from pydantic import BaseModel, field_validator

from idp_common import document_attributes
from idp_common.config.prefix_mappings import (
    SOURCE_ACTIVE_PROFILE,
    SOURCE_DOCUMENT_PIN,
    SOURCE_EXPLICIT_REQUEST,
    SOURCE_PREFIX_MAPPING,
    PrefixMappingStore,
    resolve_config_assignment,
)
from idp_common.config_scope import scope_allows
from idp_common.document_versions import delete_current_output_objects
from idp_common.models import Document, Status, attributes_dropped_issue
from idp_common.review_queues.exceptions import ReviewConflict

from .clearing import clear_for_start
from .eligibility import (
    OUTPUT_MISSING,
    Eligibility,
    EligibilityContext,
    ReprocessRequest,
    check,
    reviewed_for,
    step_options,
)
from .mode import PIPELINE, ProcessingMode, resolve_processing_mode
from .models import REVIEW_KINDS, RunKind, RunPlan, encode
from .resolver_requests import FAILED, QUEUED, REFUSED
from .restart import SEND_BACK_START
from .sender import EVENT_DOCUMENT_REPROCESSED, send_run
from .steps import BEGINNING, steps_for_mode

logger = logging.getLogger(__name__)

__all__ = [
    "FAILED",
    "ORIGIN_KINDS",
    "QUEUED",
    "REFUSED",
    "InputObjectMissing",
    "ReprocessOrigin",
    "ReprocessOutcome",
    "ReprocessService",
]

#: The kinds of run this service queues.
ORIGIN_KINDS = frozenset({RunKind.REPROCESS, RunKind.SENT_BACK, RunKind.RESTART_FAILED})

#: The profile name the queue processor uses when none is active.
_DEFAULT_PROFILE = "default"

#: ``metrics(name, dimensions)``: how the service publishes a count.
Metrics = Callable[[str, Optional[Dict[str, Any]]], None]


class InputObjectMissing(ValueError):
    """The document's file is not in the input bucket, or cannot be read."""


class ReprocessOutcome(BaseModel):
    """What happened to one document of a request."""

    object_key: str
    status: Literal["QUEUED", "REFUSED", "FAILED"]
    #: The step the run starts at, after aliases and ``resume``.
    start_step: str = ""
    #: An eligibility code when refused; the error's class name when failed.
    reason: str = ""
    message: str = ""


class ReprocessOrigin(BaseModel):
    """Why a run is asked for, when it is not a plain reprocess."""

    kind: RunKind = RunKind.REPROCESS
    #: Who asked, for the review history of a send-back.
    actor: str = ""
    #: The review cycle a send-back leaves.
    cycle_id: str = ""
    note: str = ""

    @field_validator("kind")
    @classmethod
    def _a_kind_this_service_queues(cls, kind: RunKind) -> RunKind:
        if kind not in ORIGIN_KINDS:
            raise ValueError(
                f"a {kind.value} run is not queued through the reprocess service"
            )
        return kind


@dataclass(frozen=True)
class _Pins:
    """The configuration a run is pinned to, and where it came from."""

    version: Optional[str]
    revision: Optional[int]
    source: Optional[str]
    mapping_prefix: Optional[str]


class _Call:
    """What one request reads, read once.

    A batch of documents that share a profile would otherwise repeat the same
    tracking-row, configuration and mapping reads per document, inside API
    Gateway's 29-second integration ceiling.
    """

    def __init__(self, service: "ReprocessService"):
        self._service = service
        self.manager = service._configuration_manager()
        #: The three seams ``resolve_config_assignment`` reads through.
        self.seams: Dict[Any, Any] = {}
        self._documents: Dict[str, Tuple[Any, Optional[Exception]]] = {}
        self._modes: Dict[Tuple[Optional[str], Optional[int]], ProcessingMode] = {}
        self._mappings: Optional[List[Any]] = None

    def document(self, object_key: str) -> Any:
        """The document as the tracking row has it. A failed read raises again."""
        if object_key not in self._documents:
            try:
                # With its policy rule validation result: a run that starts
                # after that stage keeps the result, and `resume` reads it.
                found = self._service._documents.get_document(
                    object_key, with_policy_rule_validation=True
                )
                self._documents[object_key] = (found, None)
            except Exception as error:  # noqa: BLE001 — re-raised to every reader
                self._documents[object_key] = (None, error)
        document, error = self._documents[object_key]
        if error is not None:
            raise error
        return document

    def profile_of(self, object_key: str) -> Optional[str]:
        """The profile a document carries, or ``None`` when it cannot be read."""
        try:
            document = self.document(object_key)
        except Exception as error:  # noqa: BLE001 — "cannot be established"
            logger.error(
                "Could not read the tracking row for a reprocess target, so its "
                "configuration profile cannot be established: %s",
                error,
            )
            return None
        return getattr(document, "config_version", None) if document else None

    def published_revision(self, profile: str) -> Optional[int]:
        if self.manager is None:
            return None
        published = _memo_published(self.manager, self.seams)
        return published(profile) if published else None

    def _active_profile(self) -> str:
        active = _memo_active(self.manager, self.seams)
        return (active() if active else None) or _DEFAULT_PROFILE

    def mode_for(
        self, profile: Optional[str], revision: Optional[int]
    ) -> ProcessingMode:
        """The processing mode of a profile at a revision.

        No profile means the active one, which is what the queue processor pins
        an unpinned document to.
        """
        key = (profile, revision)
        if key not in self._modes:
            if self.manager is None:
                self._modes[key] = ProcessingMode(PIPELINE, None, None, False)
            else:
                self._modes[key] = resolve_processing_mode(
                    self.manager, profile or self._active_profile(), revision
                )
        return self._modes[key]

    def mappings(self) -> List[Any]:
        if self._mappings is None:
            self._mappings = self._service._prefix_mappings(self.manager)
        return self._mappings

    def context(self) -> EligibilityContext:
        return EligibilityContext(
            load_document=self.document,
            mode_for=self.mode_for,
            published_revision=self.published_revision,
        )


class ReprocessService:
    """Queues documents to run again.

    Args:
        document_service: the tracking-table service (``DocumentDynamoDBService``).
        sqs_client: the client the message is sent with.
        queue_url: the document queue.
        s3: the S3 client used to check the input object and to delete output.
        input_bucket, output_bucket: the stack's buckets.
        working_bucket: where the document of a later start is stored, with a
            reference in the message. Without one the message carries the
            document itself.
        config_manager_factory: returns a ``ConfigurationManager``, or ``None``
            when no configuration table is wired.
        review_service_factory: returns the review-queue service, or ``None``
            when the stack has no review table.
        metrics: ``metrics(name, dimensions)``, to publish a count.
        retention_days: how long a recreated tracking row is kept.
    """

    def __init__(
        self,
        *,
        document_service: Any,
        sqs_client: Any,
        queue_url: str,
        s3: Any,
        input_bucket: str,
        output_bucket: str,
        working_bucket: Optional[str] = None,
        config_manager_factory: Optional[Callable[[], Any]] = None,
        review_service_factory: Optional[Callable[[], Any]] = None,
        metrics: Optional[Metrics] = None,
        retention_days: int = 365,
    ):
        self._documents = document_service
        self._sqs = sqs_client
        self._queue_url = queue_url
        self._s3 = s3
        self._input_bucket = input_bucket
        self._output_bucket = output_bucket
        self._working_bucket = working_bucket
        self._config_manager_factory = config_manager_factory
        self._review_service_factory = review_service_factory
        self._metrics = metrics
        self._retention_days = retention_days

    # ----- the two operations ------------------------------------------------

    def reprocess(
        self,
        object_keys: Sequence[str],
        *,
        start: str = BEGINNING,
        skip: Sequence[str] = (),
        version: Optional[str] = None,
        revision: Optional[int] = None,
        allowed_versions: Optional[Sequence[str]] = None,
        origin: Optional[ReprocessOrigin] = None,
    ) -> List[ReprocessOutcome]:
        """Queue each document to run again, and say what happened to each.

        Every document is checked, and the configuration of every accepted
        document decided, before any is queued. A refused document is not
        queued and does not stop the others; neither does one whose storage or
        send fails.

        ``version`` and ``revision`` apply to a plain reprocess. A send-back and
        a restart of a failed run keep the revision the document is pinned to.

        A send-back has one start, whatever ``start`` says: classification
        (``SEND_BACK_START``), which is the beginning in BDA mode. A document
        that lacks what that start reads is sent back to the beginning.

        Raises:
            PermissionError: a document, the named profile, or the profile a
                prefix mapping assigns is outside ``allowed_versions``.
            ValueError: a prefix mapping set to refuse a conflicting profile
                governs a document. Nothing is queued in either case.
        """
        origin = origin or ReprocessOrigin()
        chooses = origin.kind == RunKind.REPROCESS
        requested_version = version if chooses else None
        requested_revision = revision if chooses else None
        skip = tuple(skip or ())
        sent_back = origin.kind == RunKind.SENT_BACK
        if sent_back:
            start = SEND_BACK_START

        call = _Call(self)
        self._refuse_outside_scope(
            call, object_keys, requested_version, allowed_versions
        )
        context = call.context()

        # Per document: the answer, or the outcome when the check itself failed.
        checked: List[Tuple[str, Union[Eligibility, ReprocessOutcome]]] = []
        for object_key in object_keys:
            request = ReprocessRequest(
                object_key=object_key,
                start=start,
                skip=skip,
                version=requested_version,
                revision=requested_revision,
                kind=origin.kind,
            )
            try:
                answer = check(request, context)
                if (
                    sent_back
                    and start != BEGINNING
                    and not answer.ok
                    and answer.code == OUTPUT_MISSING
                ):
                    # The page text the start reads is not there, so the
                    # document is sent back to the beginning instead.
                    answer = check(replace(request, start=BEGINNING), context)
                checked.append((object_key, answer))
            except Exception as error:  # noqa: BLE001 — one document, not the batch
                checked.append((object_key, self._failed(object_key, "", error)))

        # Decided for every accepted document before any is queued: a refusal
        # here is for the request, and a part-applied routing policy is harder to
        # reason about than a refused request.
        pins: Dict[str, _Pins] = {}
        for object_key, answer in checked:
            if isinstance(answer, Eligibility) and answer.ok:
                pins[object_key] = self._pins_for(
                    call,
                    object_key,
                    answer,
                    requested_version,
                    requested_revision,
                    allowed_versions,
                    chooses,
                )

        outcomes: List[ReprocessOutcome] = []
        for object_key, answer in checked:
            if isinstance(answer, ReprocessOutcome):
                outcomes.append(answer)
                continue
            if not answer.ok:
                logger.warning(
                    "Not reprocessing %s: %s %s",
                    object_key,
                    answer.code,
                    answer.message,
                )
                outcomes.append(
                    ReprocessOutcome(
                        object_key=object_key,
                        status=REFUSED,
                        start_step=answer.start,
                        reason=answer.code,
                        message=answer.message,
                    )
                )
                continue
            try:
                self._queue(object_key, answer, pins[object_key], skip, origin)
            except ReviewConflict as conflict:
                # A send-back of a review that was replaced after the reviewer
                # opened it. Nothing was changed for this document.
                logger.warning("Not reprocessing %s: %s", object_key, conflict)
                outcomes.append(
                    ReprocessOutcome(
                        object_key=object_key,
                        status=REFUSED,
                        start_step=answer.start,
                        reason=conflict.code,
                        message=conflict.message,
                    )
                )
                continue
            except Exception as error:  # noqa: BLE001 — continue with the others
                outcomes.append(self._failed(object_key, answer.start, error))
                continue
            outcomes.append(
                ReprocessOutcome(
                    object_key=object_key, status=QUEUED, start_step=answer.start
                )
            )
        return outcomes

    def options(
        self,
        object_keys: Sequence[str],
        *,
        version: Optional[str] = None,
        revision: Optional[int] = None,
        allowed_versions: Optional[Sequence[str]] = None,
    ) -> List[Dict[str, Any]]:
        """For each document: its mode, and the steps a run can start at.

        Reads only. Each entry is ``{"objectKey", "mode", "defaultStep",
        "steps"}``, and each step ``{"id", "available", "skippable",
        "reason"}``, where ``reason`` is the eligibility code that refuses the
        step.
        """
        call = _Call(self)
        self._refuse_outside_scope(call, object_keys, version, allowed_versions)
        context = call.context()
        options = []
        for object_key in object_keys:
            try:
                document = call.document(object_key)
            except Exception:  # noqa: BLE001 — every step then reports the gate
                document = None
            mode = call.mode_for(
                getattr(document, "config_version", None) if document else None,
                getattr(document, "config_revision", None) if document else None,
            )
            request = ReprocessRequest(
                object_key=object_key, version=version, revision=revision
            )
            options.append(
                {
                    "objectKey": object_key,
                    "mode": mode.mode,
                    "defaultStep": BEGINNING,
                    "steps": step_options(
                        document, mode, context, request, steps_for_mode(mode.mode)
                    ),
                }
            )
        return options

    # ----- scope --------------------------------------------------------------

    def _refuse_outside_scope(
        self,
        call: _Call,
        object_keys: Sequence[str],
        version: Optional[str],
        allowed_versions: Optional[Sequence[str]],
    ) -> None:
        """Refuse the request when it reaches outside the caller's scope.

        Both directions, and both fail closed: the profile the documents would
        run under, and the profile each document was last processed under. A
        document whose profile cannot be read cannot be proven in scope.
        """
        if not allowed_versions:
            return
        if version and not scope_allows(allowed_versions, version):
            logger.warning(
                "Rejecting the reprocess: the caller is scoped to %s but requested "
                "version=%r",
                sorted(allowed_versions),
                version,
            )
            raise PermissionError(
                f"Access denied: version '{version}' is not in your allowed scope"
            )
        for object_key in object_keys:
            current = call.profile_of(object_key)
            if not scope_allows(allowed_versions, current):
                logger.warning(
                    "Rejecting the reprocess: a requested document was processed "
                    "under config version %r, and the caller is scoped to %s",
                    current,
                    sorted(allowed_versions),
                )
                raise PermissionError(
                    "Access denied: one or more of the requested documents is "
                    "outside your allowed configuration scope"
                )

    # ----- configuration ------------------------------------------------------

    def _configuration_manager(self) -> Any:
        if self._config_manager_factory is None:
            return None
        return self._config_manager_factory()

    def _pins_for(
        self,
        call: _Call,
        object_key: str,
        answer: Eligibility,
        requested_version: Optional[str],
        requested_revision: Optional[int],
        allowed_versions: Optional[Sequence[str]],
        chooses: bool,
    ) -> _Pins:
        """The configuration one accepted document runs under.

        A reprocess from the beginning is assigned a configuration the way a
        new upload is: see ``_version_for_document``. A later start keeps the
        document's own profile, which ``eligibility.check`` has enforced, at the
        revision the caller named or else the profile's published revision.
        Prefix mappings are not consulted for it: they assign a profile, and a
        later start does not change the profile. A send-back and a restart keep
        the pinned revision too.
        """
        document = answer.document
        if chooses and answer.start == BEGINNING:
            # A scoped caller's reprocess stays pinned to the document's own
            # profile, which `_refuse_outside_scope` has just verified.
            pinned = (
                getattr(document, "config_version", None) if allowed_versions else None
            )
            return _Pins(
                *self._version_for_document(
                    requested_version,
                    requested_revision,
                    object_key,
                    pinned,
                    allowed_versions,
                    mappings=call.mappings(),
                    manager=call.manager,
                    seam_cache=call.seams,
                )
            )

        profile = getattr(document, "config_version", None)
        revision = getattr(document, "config_revision", None)
        if chooses:
            # The revision the eligibility check judged the request against.
            if requested_revision is not None:
                revision = requested_revision
            elif profile:
                published = call.published_revision(profile)
                revision = published if published is not None else revision
        return _Pins(
            profile,
            revision,
            getattr(document, "config_source", None),
            getattr(document, "config_mapping_prefix", None),
        )

    def _prefix_mappings(self, manager: Any) -> List[Any]:
        """The deployment's config prefix mappings, or ``[]`` if unavailable.

        Fails open, matching ``queue_sender``: reprocessing a document under the
        profile it already has, or under the active one, is a far better outcome
        than refusing the reprocess because a *routing* table could not be read.
        """
        if manager is None:
            # Same signal as a read failure, for the same reason: the two branches
            # produce the identical outcome (no mapping applies) and only one of
            # them was visible.
            logger.error(
                "No configuration table is wired, so no prefix mapping can be "
                "applied on reprocess"
            )
            self._emit("PrefixMappingLookupFailed")
            return []
        try:
            return PrefixMappingStore(manager.table).list()
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Could not read the configuration prefix mappings (%s); reprocessing "
                "without them",
                error,
            )
            self._emit("PrefixMappingLookupFailed")
            return []

    def _version_for_document(
        self,
        requested_version: Optional[str],
        requested_revision: Optional[int],
        object_key: str,
        pinned: Optional[str],
        allowed_versions: Optional[Sequence[str]] = None,
        mappings: Optional[List[Any]] = None,
        manager: Any = None,
        seam_cache: Optional[Dict[Any, Any]] = None,
    ) -> Tuple[Optional[str], Optional[int], Optional[str], Optional[str]]:
        """The configuration to reprocess one document under, and where it came from.

        Returns ``(version, revision, source, mapping_prefix)``.

        The order is a precedence chain and each step is there for a different
        reason:

        1. **An explicit ``version`` argument is adjudicated by the mapping, not
           exempt from it.** It is a deliberate choice by an authenticated caller
           and it has already been scope-checked -- but so is a profile named in
           upload metadata, and a mapping's conflict mode is precisely the rule
           for what happens when the two disagree. Returning the request
           unconditionally would make ``mapping`` precedence mean "the mapping
           wins, except on reprocess" and make ``reject`` mean "refuse the
           upload, then accept the same conflict on a re-run" -- so an operator
           could defeat an administrator's routing policy by uploading, letting
           it be refused or overridden, and reprocessing with the profile they
           wanted. The request therefore goes through
           ``resolve_config_assignment`` as the metadata profile, which applies
           the same three modes the upload path applies, including refusing the
           reprocess outright under ``reject``.
        2. **A scoped caller's reprocess stays pinned to the document's own
           profile** (``pinned``), which the scope check has just verified.
           ⚠️ This step must come *before* the prefix mapping, and the reason is
           easy to get backwards: the pin exists for exactly the case "no
           explicit profile was requested", which is also the case a mapping
           would like to fill. Consulting the mapping first would let a
           reprocess move a scoped caller's own document out of their scope.
        3. **Then a prefix mapping**, so that moving a mapping and reprocessing
           is how an operator re-runs a backlog under a new profile. Only
           reachable for an unscoped caller or an Admin, and the resolved
           profile is scope-checked anyway so the rule holds even if (2) ever
           stops producing a pin.
        4. **Otherwise unpinned**, and ``queue_processor`` resolves the active
           profile.

        ⚠️ **The revision travels with the profile it belongs to.** Revision
        numbers are per profile, so a caller's ``revision`` argument means
        nothing against a profile the caller did not name. Passing one through
        alongside a mapping-supplied profile would stamp, say, r5 of profile A
        onto profile B. So a mapping supplies both or neither, and ``revision``
        is honoured only where ``version`` came from the caller or from the
        document's own pin.
        """
        if requested_version:
            # No mapping set, or no mapping governs this key: the request stands,
            # which is the overwhelmingly common case.
            if not mappings:
                return (
                    requested_version,
                    requested_revision,
                    SOURCE_EXPLICIT_REQUEST,
                    None,
                )

            seam_cache = {} if seam_cache is None else seam_cache
            decided = resolve_config_assignment(
                object_key,
                # The caller's request takes the metadata slot, because it is the
                # same kind of claim -- "process this under X" from whoever
                # initiated it -- and `metadataPrecedence` is the administrator's
                # rule for adjudicating it against the prefix.
                metadata_profile=requested_version,
                metadata_revision=requested_revision,
                mappings=mappings,
                active_profile=_memo_active(manager, seam_cache),
                published_revision=_memo_published(manager, seam_cache),
                profile_exists=_memo_exists(manager, seam_cache),
                allowed_profiles=list(allowed_versions) if allowed_versions else None,
            )
            if decided.rejected and decided.unresolvable:
                # Refused rather than reprocessed under the caller's profile
                # (#597): the mapping for this prefix names a profile that does
                # not exist, and that is a broken routing rule for the admin to
                # fix, not a contest the request wins by default. Same marker and
                # metric as ingest.
                logger.warning(
                    "Stale prefix mapping for %s: %s", object_key, decided.reason
                )
                self._emit("PrefixMappingUnresolvable")
                raise ValueError(
                    _unresolvable_refusal(object_key, decided.mapping_prefix)
                )
            if decided.rejected:
                self._emit("PrefixMappingRejected")
                logger.warning(
                    "Refusing to reprocess %s: %s", object_key, decided.reason
                )
                # Reworded rather than passed through. The resolver's own reason
                # is written for the ingest path and says the object "carries
                # conflicting upload METADATA" -- true there, and wrong here,
                # where the conflicting profile came from this request rather
                # than from the object. An operator reading it would go looking
                # at the object's metadata for a value that is not on it.
                #
                # The PREFIX, not the profile: a rejected assignment deliberately
                # carries no profile, and the prefix is both available and the
                # actionable half -- it is what the admin edits to change the
                # rule.
                raise ValueError(
                    f"Refused: {object_key!r} is governed by the configuration "
                    f"prefix mapping for {decided.mapping_prefix!r}, which is set "
                    f"to refuse a conflicting profile rather than choose between "
                    f"them. Reprocess without naming a profile to use the "
                    f"mapping's own, or change that mapping's conflict mode."
                )
            if decided.scope_denied:
                # A mapping may resolve a profile outside the caller's scope even
                # though their own request was in scope. Refusing is the same
                # answer the upload path gives.
                logger.warning(
                    "Refusing to reprocess %s: the resolved Configuration Profile "
                    "is outside the caller's configuration scope",
                    object_key,
                )
                raise PermissionError(
                    "Unauthorized: that document's destination is governed by a "
                    "Configuration Profile outside your allowed configuration scope."
                )
            # ⚠️ Emission conditions are `queue_sender`'s, verbatim, and must stay
            # that way. Both emitters feed one dashboard expression
            # (`SEARCH('{<ns>,Prefix} ...')`), so a metric that means "matched the
            # key" at ingest and "won the contest" here makes a prefix under
            # `metadata` precedence show ingest hits and no reprocess hits --
            # which reads as "that prefix is not used on the reprocess path"
            # rather than as two definitions.
            #
            # `Applied` therefore fires whenever a mapping MATCHED, whoever won,
            # and `Conflict` only on an actual disagreement -- an explicit request
            # naming the profile the mapping assigns is agreement, not a conflict.
            if decided.unresolvable:
                # Same marker string the other two emitters log, because
                # PrefixMappingUnresolvableAlarm sends an operator looking for it.
                logger.warning(
                    "Stale prefix mapping for %s: %s", object_key, decided.reason
                )
                self._emit("PrefixMappingUnresolvable")
            if decided.mapping_prefix:
                self._emit("PrefixMappingApplied", {"Prefix": decided.mapping_prefix})
            if decided.conflict:
                self._emit("PrefixMappingConflict", {"Winner": decided.source})
                logger.warning(
                    "Configuration conflict for %s: %s", object_key, decided.reason
                )

            if decided.mapping_prefix and decided.source == SOURCE_PREFIX_MAPPING:
                # The mapping won, so it supplies profile AND revision -- revision
                # numbers are per profile, so the caller's cannot travel.
                logger.info("Reprocess of %s: %s", object_key, decided.reason)
                return (
                    decided.profile,
                    decided.revision,
                    SOURCE_PREFIX_MAPPING,
                    decided.mapping_prefix,
                )
            return (
                requested_version,
                requested_revision,
                SOURCE_EXPLICIT_REQUEST,
                decided.mapping_prefix,
            )

        if pinned:
            # The caller named no profile, so a `revision` argument is theirs to
            # apply to the document's existing one -- the pair the UI's reprocess
            # dialog sends.
            return pinned, requested_revision, SOURCE_DOCUMENT_PIN, None

        if not mappings:
            # `active-profile`, not an empty source. The profile is left None so
            # the queue processor resolves whatever is active when the document
            # is picked up, but the PROVENANCE is known now and is the whole
            # point of the field: a blank ConfigSource is indistinguishable from
            # a document written before the field existed.
            return None, requested_revision, SOURCE_ACTIVE_PROFILE, None

        seam_cache = {} if seam_cache is None else seam_cache
        assignment = resolve_config_assignment(
            object_key,
            mappings=mappings,
            active_profile=_memo_active(manager, seam_cache),
            published_revision=_memo_published(manager, seam_cache),
            profile_exists=_memo_exists(manager, seam_cache),
            # Scope is evaluated on the RESOLVED profile. For an unscoped caller
            # `allowed_versions` is empty, which `scope_allows` reads as
            # unrestricted.
            allowed_profiles=list(allowed_versions) if allowed_versions else None,
        )
        if assignment.unresolvable:
            # Logged as well as emitted, with the same marker queue_sender uses.
            # The alarm description sends an operator looking for this string,
            # and the metric has two emitters.
            logger.warning(
                "Stale prefix mapping for %s: %s", object_key, assignment.reason
            )
            self._emit("PrefixMappingUnresolvable")
        if assignment.rejected:
            # Only reachable through an unresolvable mapping: with no profile
            # requested there is nothing for a `reject` mapping to conflict with.
            # Refused rather than reprocessed under the active profile (#597).
            raise ValueError(
                _unresolvable_refusal(object_key, assignment.mapping_prefix)
            )
        if assignment.scope_denied or not assignment.mapping_prefix:
            if assignment.scope_denied:
                logger.warning(
                    "Not applying a prefix mapping to %s on reprocess: the mapped "
                    "profile is outside the caller's configuration scope",
                    object_key,
                )
            # Also `active-profile`: no mapping applied, so the document
            # reprocesses the way it would have with none configured. The scope
            # denial is recorded in the log above rather than in the source,
            # deliberately -- the source travels onto the document and is read
            # back by anyone who can see it, so a value meaning "a mapping was
            # refused here" would tell a scoped user that a mapping governs a
            # prefix they cannot see.
            return None, requested_revision, SOURCE_ACTIVE_PROFILE, None

        logger.info("Reprocess of %s: %s", object_key, assignment.reason)
        self._emit("PrefixMappingApplied", {"Prefix": assignment.mapping_prefix})
        # Both, or neither: the mapping's revision, never the caller's.
        return (
            assignment.profile,
            assignment.revision,
            assignment.source,
            assignment.mapping_prefix,
        )

    # ----- queueing -----------------------------------------------------------

    def _queue(
        self,
        object_key: str,
        answer: Eligibility,
        pins: _Pins,
        skip: Sequence[str],
        origin: ReprocessOrigin,
    ) -> str:
        """Queue one accepted document, and return the message id.

        In this order: the plan is built, so a plan that cannot be sent raises
        before anything is changed; the input object is checked; a live review
        is withdrawn; the outputs are cleared and the tracking row written; and
        the message is sent.
        """
        existing = answer.document
        start = answer.start
        logger.info(
            "Reprocessing document: %s from %s%s%s",
            object_key,
            start,
            f" with version: {pins.version}" if pins.version else "",
            f" r{pins.revision}" if pins.revision is not None else "",
        )
        plan = RunPlan(
            kind=origin.kind,
            start=start,
            skip=sorted(set(skip)),
            sections=list(answer.sections),
            # A send-back asks for the review again, whatever came before.
            reviewed=(
                origin.kind != RunKind.SENT_BACK
                and existing is not None
                and reviewed_for(existing, start)
            ),
            cycle_id=origin.cycle_id if origin.kind in REVIEW_KINDS else "",
        )
        encode(plan)

        head = self._require_input_object(object_key)
        withdrawn = self._withdraw_review(object_key, origin, start)

        if start == BEGINNING:
            document = self._start_over(object_key, existing, pins, head)
            working_bucket = None
        else:
            if withdrawn:
                # The withdrawal removed the review status from the tracking row.
                # This document was read before that, and the row and the run are
                # written from it: its copy would put the status back, and a run
                # that is not held again would end with a review nobody has.
                existing.hitl_status = None
            document = self._start_later(existing, answer, pins)
            working_bucket = self._working_bucket

        message_id = send_run(
            self._sqs,
            self._queue_url,
            document=document,
            plan=plan,
            event_type=EVENT_DOCUMENT_REPROCESSED,
            working_bucket=working_bucket,
            prefix="reprocess",
        )
        logger.info("Successfully reprocessed document: %s", object_key)
        return message_id

    def _require_input_object(self, object_key: str) -> Dict[str, Any]:
        """Check the input object is there, and return what ``head_object`` says."""
        try:
            return self._s3.head_object(Bucket=self._input_bucket, Key=object_key)
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Could not find %s in the input bucket %s: %s",
                object_key,
                self._input_bucket,
                error,
            )
            raise InputObjectMissing(
                f"Document {object_key} not found in S3 bucket {self._input_bucket}"
            ) from error

    def _review_service(self) -> Any:
        if self._review_service_factory is None:
            return None
        return self._review_service_factory()

    def _withdraw_review(
        self,
        object_key: str,
        origin: Optional[ReprocessOrigin] = None,
        start: str = "",
    ) -> bool:
        """Withdraw a live review item before the run (the new run routes again).

        Returns True when a live review was withdrawn.

        A send-back is recorded in the review's history as ``sent_back``, with
        who sent it back, the step the run starts at and the note, and it is
        withdrawn only if the live review is still the cycle the reviewer acted
        on: a review that has been replaced since raises ``ReviewConflict``
        (``CYCLE_CHANGED``) and nothing is changed. Any other reprocess records
        ``withdrawn``.
        """
        service = self._review_service()
        if service is None:
            return False
        try:
            if origin is not None and origin.kind == RunKind.SENT_BACK:
                return bool(
                    service.withdraw(
                        object_key,
                        "sent_back",
                        actor=origin.actor or None,
                        expected_cycle=origin.cycle_id or None,
                        history_event="sent_back",
                        details={"start_step": start, "note": origin.note},
                    )
                )
            return bool(service.withdraw(object_key, "reprocessed"))
        except ReviewConflict:
            raise
        except Exception:
            # The fresh run's routing supersedes a leftover item, so a failure
            # here never blocks the reprocess; it is logged for the operator.
            logger.error(
                "Failed to withdraw the review item before reprocess", exc_info=True
            )
            return False

    def _delete_output_data(self, input_key: str) -> None:
        """Delete previous processing output from the output bucket.

        A run from the beginning must not find OCR results a previous run left:
        the OCR function's retry-safe recovery (``discover_existing_ocr_pages``)
        would pick them up and skip OCR instead of running it with the current
        configuration.

        ⚠️ Broad purge: this calls ``delete_current_output_objects`` with the
        default ``subprefixes=None`` (everything under ``<key>/`` except
        ``<key>/runs/``). Where object keys are nested (``foo`` reprocessed while
        a separate document lives at ``foo/bar.pdf``), the purge deletes the
        nested document's output too, because only this document's ``runs/`` is
        preserved. That is accepted for a reprocess from the beginning, which
        is a deliberate "start over".
        """
        try:
            deleted = delete_current_output_objects(
                self._s3, self._output_bucket, input_key
            )
            if deleted:
                logger.info(
                    "Deleted %d objects from s3://%s/%s/",
                    deleted,
                    self._output_bucket,
                    input_key,
                )
            else:
                logger.info("No previous output data found for %s", input_key)
        except Exception as error:  # noqa: BLE001
            # Non-fatal: OCR still runs but may recover stale partial data. Logged
            # at ERROR and published as a metric, so an operator can alarm on it
            # without scraping logs, as `queue_sender` does for the same symptom
            # on the ingest path. A partial purge means the run does not start
            # over cleanly: one surviving complete page is enough for OCR's
            # recovery to bring back stale text.
            logger.error(
                "Failed to delete previous output data for %s: %s", input_key, error
            )
            self._emit("StaleOutputPurgeFailed")

    def _start_over(
        self,
        object_key: str,
        existing: Any,
        pins: _Pins,
        head: Optional[Dict[str, Any]] = None,
    ) -> Document:
        """Purge the output and recreate the tracking row for a run from the start.

        ``head`` is what ``head_object`` said about the input object.
        """
        self._delete_output_data(object_key)

        # Caller-supplied attributes (#1851) are carried unchanged: the snapshot
        # the original intake stored on the row. Only when there is no row (it
        # expired, or the document was never ingested) are they read from the
        # object again, by the same rules intake uses.
        attribute_issues = []
        if existing is not None:
            attributes = dict(getattr(existing, "attributes", None) or {})
            # ...and so is the warning that intake dropped some, which describes
            # the same snapshot.
            attribute_issues = [
                issue
                for issue in (getattr(existing, "processing_issues", None) or [])
                if getattr(issue, "code", None)
                == document_attributes.ISSUE_ATTRIBUTES_DROPPED
            ]
        else:
            intake = document_attributes.read_object_attributes(
                (head or {}).get("Metadata") or {},
                self._input_bucket,
                self._s3,
                document_key=object_key,
            )
            attributes = intake.attributes
            if intake.dropped:
                attribute_issues.append(attributes_dropped_issue(intake.dropped))

        now = datetime.now(timezone.utc)
        current_time = now.isoformat()
        document = Document(
            id=object_key,
            input_bucket=self._input_bucket,
            input_key=object_key,
            output_bucket=self._output_bucket,
            status=Status.QUEUED,
            queued_time=current_time,
            initial_event_time=current_time,
            pages={},
            sections=[],
            config_version=pins.version,
            config_revision=pins.revision,
            config_source=pins.source,
            config_mapping_prefix=pins.mapping_prefix,
            # A Test Studio document stays one: the row is recreated, and these
            # two say which list it belongs to and which test set it is part of.
            submission_source=getattr(existing, "submission_source", None),
            test_set_id=getattr(existing, "test_set_id", None),
            attributes=attributes,
            processing_issues=attribute_issues,
        )
        expires_after = int((now + timedelta(days=self._retention_days)).timestamp())
        self._documents.create_document(document, expires_after=expires_after)
        logger.info("Created fresh document object for reprocessing: %s", object_key)
        return document

    def _start_later(self, document: Any, answer: Eligibility, pins: _Pins) -> Any:
        """Clear the outputs of the start step and later, and update the row."""
        document.input_bucket = self._input_bucket
        document.output_bucket = self._output_bucket
        removed = clear_for_start(
            document,
            answer.start,
            sections=answer.sections,
            s3=self._s3,
            output_bucket=self._output_bucket,
        )
        document.status = Status.QUEUED
        document.start_time = None
        document.completion_time = None
        document.workflow_execution_arn = None
        document.config_revision = pins.revision
        self._documents.update_document(document)
        if removed:
            # `update_document` sets attributes and removes none, so what was
            # cleared would stay on the row.
            self._documents.remove_attributes(document.input_key, removed)
        return document

    # ----- reporting ----------------------------------------------------------

    def _failed(
        self, object_key: str, start_step: str, error: Exception
    ) -> ReprocessOutcome:
        logger.error(
            "Error reprocessing document %s: %s", object_key, error, exc_info=True
        )
        # The error's own text can carry a service's message, which is not for
        # the caller; the log has it.
        message = (
            str(error)
            if isinstance(error, InputObjectMissing)
            else f"Could not queue {object_key} to run again."
        )
        return ReprocessOutcome(
            object_key=object_key,
            status=FAILED,
            start_step=start_step,
            reason=type(error).__name__,
            message=message,
        )

    def _emit(self, name: str, dimensions: Optional[Dict[str, Any]] = None) -> None:
        """Publish a count. Telemetry never affects a reprocess."""
        if self._metrics is None:
            return
        try:
            self._metrics(name, dimensions)
        except Exception as error:  # noqa: BLE001
            logger.warning("Could not publish the metric %s: %s", name, error)


def _unresolvable_refusal(object_key: str, mapping_prefix: Optional[str]) -> str:
    """The reprocess refusal for a mapping that names a profile that does not exist.

    Names the prefix rather than the profile, as the conflict refusal does: the
    prefix is what the admin edits to fix it. A caller outside the scope of the
    mapped profile never reaches this: the resolver reports a scope denial first.
    """
    return (
        f"Refused: {object_key!r} is governed by the configuration prefix mapping "
        f"for {mapping_prefix!r}, which names a Configuration Profile that does not "
        f"exist. Recreate that profile, or change or delete the mapping, then "
        f"reprocess."
    )


def _profile_head_exists(manager: Any, profile: str) -> bool:
    """Whether a profile head item is there, without reading its body."""
    item = manager.table.get_item(
        Key={"Configuration": f"Config#{profile}"},
        ProjectionExpression="Configuration",
    ).get("Item")
    return bool(item)


# The three resolver seams, memoized for one request. A batch reprocess of N
# documents sharing one prefix would otherwise spend up to 3N DynamoDB reads whose
# answers are identical, inside API Gateway's 29s integration ceiling -- which is
# the shape that turns a constant factor into a timeout on a large batch.


def _memo_active(manager: Any, cache: Dict[Any, Any]) -> Optional[Callable[[], Any]]:
    if manager is None:
        return None

    def active() -> Any:
        if "active" not in cache:
            cache["active"] = manager.resolve_active_version()
        return cache["active"]

    return active


def _memo_published(
    manager: Any, cache: Dict[Any, Any]
) -> Optional[Callable[[str], Optional[int]]]:
    if manager is None:
        return None

    def published(profile: str) -> Optional[int]:
        key = ("published", profile)
        if key not in cache:
            cache[key] = manager.resolve_published_revision(profile)
        return cache[key]

    return published


def _memo_exists(
    manager: Any, cache: Dict[Any, Any]
) -> Optional[Callable[[str], bool]]:
    if manager is None:
        return None

    def exists(profile: str) -> bool:
        key = ("exists", profile)
        if key not in cache:
            cache[key] = _profile_head_exists(manager, profile)
        return cache[key]

    return exists
