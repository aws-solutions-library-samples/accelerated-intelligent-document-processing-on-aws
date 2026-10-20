# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.7 -> v0.8: ``classificationMethod`` becomes ``granularity``.

``classification.classificationMethod`` had two values and each one bundled two
independent choices — **how many calls are made** and **what each call sees**:

* ``multimodalPageLevelClassification`` = one call per page, with text and images
* ``textbasedHolisticClassification``   = one call per packet, text only

Four of the six combinations therefore had no setting at all, and the two that did
could only be reached by editing a placeholder out of the task prompt.
``classification.granularity`` and ``classification.modality`` replace it
(GitHub #1328), and this migration removes the old key rather than leaving it as
a live alias — the v0.5 and v0.6 precedent. Two readable homes for one setting is
a bug factory: a config carrying both would need a silent precedence rule, and
every future reader would have to know about it. It is also what the field was
*about* to become, since a dropdown that says "deprecated, ignored when
granularity is set" is a worse UI than no dropdown.

Mapping, and why each half preserves behaviour exactly
-----------------------------------------------------

**Granularity** is a direct rename of the one choice the method really made:
holistic → ``packet``, anything else → ``page``. An unrecognised value took the
page-level path with a warning, and still does.

**Modality is deliberately NOT written.** An unset flag is inferred from the
prompt's placeholders — ``{DOCUMENT_TEXT}`` present means text is sent,
``{DOCUMENT_IMAGE}`` present means the image is — which is precisely what the
service did before the setting existed, including for the documented workaround
of deleting a placeholder to get one modality. Writing ``text: true, images:
true`` to match the method's *name* would therefore be a behaviour CHANGE for
every config that used that workaround: it would start sending images to a prompt
that had been edited not to ask for them. Leaving modality unset is the only
choice that is a no-op.

⚠️ **The prompts are the hard half, and the reason this migration is not a
one-line rename.** ``task_prompt`` was a single key shared by both methods, and
no packet-shaped default prompt has ever existed in this repository — not in
``base-classification.yaml`` and not in any preset. So the only way holistic ever
worked was a **hand-written, packet-shaped** flat ``task_prompt``. Setting
``granularity: packet`` alone would make the service prefer
``classification.packet.task_prompt``, which for such a config is the shipped
default — silently replacing the operator's own prompt with ours, on a path where
the output shape is what the parser depends on. So for a holistic config the flat
prompts are **copied into** ``classification.packet``, where the packet path reads
them. A page-level config's prompts are left exactly where they are.
"""

import copy
import logging
from typing import Any, Dict

from ._version import is_at_or_after, is_newer_than

logger = logging.getLogger(__name__)

TARGET_VERSION = "0.8"

#: The legacy value that meant "one call per packet".
LEGACY_PACKET_METHOD = "textbasedholisticclassification"
#: The legacy value that meant "one call per page" — the default.
LEGACY_PAGE_METHOD = "multimodalpagelevelclassification"


def _has_legacy_markers(config: Dict[str, Any]) -> bool:
    """True if the config still carries ``classification.classificationMethod``.

    This is the load-bearing trigger, not the version stamp. The deep-merge path
    can produce a dict stamped with the CURRENT version (inherited from the full
    default) that still carries a legacy-shaped delta from a sparse custom
    override, and relying on the stamp alone would skip such a hybrid and leave
    the user's method setting to be silently dropped by the model.
    """
    classification = config.get("classification")
    return isinstance(classification, dict) and "classificationMethod" in classification


def _needs_migration(config: Dict[str, Any]) -> bool:
    """True when the config predates v0.8 or still carries the legacy key."""
    if not is_at_or_after(config.get("config_format_version"), TARGET_VERSION):
        return True
    return _has_legacy_markers(config)


def migrate_v07_to_v08(config: Dict[str, Any]) -> Dict[str, Any]:
    """Return a v0.8-shaped copy of ``config``.

    Operates only on keys that are present, so it works on both full configs and
    sparse override deltas without injecting unrelated defaults. Idempotent.
    """
    if not isinstance(config, dict):
        return config

    if not _needs_migration(config):
        # Preserve object identity for the pure no-op case, matching the
        # earlier steps in the chain.
        return config

    result = copy.deepcopy(config)

    classification = result.get("classification")
    if isinstance(classification, dict) and "classificationMethod" in classification:
        method = classification.pop("classificationMethod")
        normalized = str(method).strip().lower() if method is not None else ""

        if normalized == LEGACY_PACKET_METHOD:
            granularity = "packet"
        else:
            if normalized not in (LEGACY_PAGE_METHOD, ""):
                # Same treatment the service gave it: an unrecognised value took
                # the page-level path with a warning rather than failing.
                logger.warning(
                    "Unrecognised classification.classificationMethod %r; "
                    "migrating to granularity: page, which is what the service "
                    "did with it",
                    method,
                )
            granularity = "page"

        # An explicit `granularity` always wins over the migrated legacy value,
        # so re-running this over a hybrid config cannot clobber a deliberate
        # setting. An EMPTY granularity is not explicit: empty meant "derive it
        # from classificationMethod", which is what this function is doing.
        if not classification.get("granularity"):
            classification["granularity"] = granularity

        if classification["granularity"] == "packet":
            _relocate_packet_prompts(classification)

        logger.info(
            "Migrated config v0.7 -> v0.8 (classification.classificationMethod "
            "%r -> classification.granularity %r)",
            method,
            classification["granularity"],
        )
        result["classification"] = classification

    # Never DOWNGRADE the stamp. Reaching here with a newer stamp means an older
    # release is reading a configuration written by a newer one (a rollback), and
    # only the legacy-marker check brought us in. Rewriting the stamp down would
    # erase the one record that this config came from a newer format, so a later
    # roll-forward would skip the migration it actually needs.
    if not is_newer_than(result.get("config_format_version"), TARGET_VERSION):
        result["config_format_version"] = TARGET_VERSION
    return result


#: What a packet-shaped task prompt must ask for. The parser reads a ``segments``
#: array of ``{ordinal_start_page, ordinal_end_page, type}``, so a prompt asking
#: for none of those keys is not a packet prompt whatever else it says.
#:
#: Anchored to the parser's **output contract** rather than to the bare word
#: "segments": a page-level prompt whose prose happens to use that word — "pages
#: are grouped into segments downstream", say — would otherwise be read as a
#: hand-written packet prompt and copied over the shipped one. Requiring a page
#: ordinal alongside it is what makes the test about the response shape.
_PACKET_PROMPT_MARKERS = ("segments", "ordinal_start_page")


def _is_packet_shaped(prompt: Any) -> bool:
    """True when a flat ``task_prompt`` is a hand-written PACKET prompt.

    This is the discriminator, and it has to be a property of the prompt rather
    than of where it sits, for two reasons that pull in opposite directions:

    * ⚠️ **"Is ``packet.task_prompt`` already set?" is not usable.** The
      migration runs on the MERGED configuration (defaults + custom delta), and
      the shipped defaults always supply ``classification.packet.task_prompt`` —
      so a guard that skips relocation when one is present fires *every* time
      and discards the operator's own prompt, which is precisely the failure the
      relocation exists to prevent.
    * **A legacy holistic config whose flat prompt was never customised was
      broken**, not working: the shipped flat default is the PAGE prompt, which
      asks for one class plus a boundary verdict and would fail every document
      against the segments parser. Carrying that forward would preserve the
      breakage; leaving it behind gets such a config the shipped packet prompt,
      which actually works.

    Asking what the prompt requests separates those cleanly: a prompt asking for
    a ``segments`` array of page ordinals is the operator's hand-written packet
    prompt and must be kept; a prompt that does not is the page default and must
    not be.
    """
    if not isinstance(prompt, str):
        return False
    return all(marker in prompt for marker in _PACKET_PROMPT_MARKERS)


def _relocate_packet_prompts(classification: Dict[str, Any]) -> None:
    """Move a holistic config's hand-written flat prompts into ``classification.packet``.

    See the module docstring: for a holistic config the flat ``task_prompt`` *is*
    the packet prompt, hand-written, because no packet-shaped default has ever
    shipped. Leaving it flat and setting ``granularity: packet`` would have the
    service read ``packet.task_prompt`` instead — ours, not theirs.

    Only a prompt that is actually packet-shaped is moved (see
    :func:`_is_packet_shaped`), and the move **overwrites** whatever
    ``packet.task_prompt`` held, because on a merged config that is the shipped
    default rather than a deliberate choice.

    The flat prompts are **copied, not cleared**, for two reasons: two-stage
    Stage B falls back to them, and a config whose granularity is later switched
    back to ``page`` would otherwise have no page prompt at all.
    """
    flat_task = classification.get("task_prompt")
    if not _is_packet_shaped(flat_task):
        logger.info(
            "The flat classification.task_prompt does not ask for a `segments` "
            "array, so it is the page-granularity prompt rather than a "
            "hand-written packet one; leaving it in place and using the shipped "
            "classification.packet.task_prompt for packet granularity"
        )
        return

    packet = classification.get("packet")
    packet = dict(packet) if isinstance(packet, dict) else {}

    # The system prompt moves with the task prompt or not at all: half of a
    # hand-written pair is worse than neither, since the two were written to
    # describe the same output shape.
    for field in ("system_prompt", "task_prompt"):
        flat = classification.get(field)
        if not isinstance(flat, str) or not flat.strip():
            continue
        packet[field] = flat
        logger.info(
            "Relocated classification.%s to classification.packet.%s: a "
            "textbasedHolisticClassification config's flat prompt IS its "
            "packet prompt, and packet granularity reads it from there",
            field,
            field,
        )

    classification["packet"] = packet
