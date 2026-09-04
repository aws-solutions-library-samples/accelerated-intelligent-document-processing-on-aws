# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Preserving hand-authored class settings when a class is regenerated.

Two write paths regenerate an existing document class from a model's output —
Discovery (``discovery/classes_discovery.py``) and BDA blueprint optimization
(``bda/blueprint_optimizer.py``) — and both used to assign the freshly
generated dict over the existing one. That erased every class-level
``x-aws-idp-*`` key an author had set, silently: the write reported success,
the class looked right in the UI (better, even, with fresh properties), and the
regression surfaced only in the *next* document processed, as a different
extraction model, a missing escalation, a re-included class, or dropped
records.

This module holds the one rule both paths use, in ``config`` rather than in
either caller so the BDA path does not have to import Discovery's dependencies.
"""

import copy
import logging
from typing import Any, Collection, Dict, List

logger = logging.getLogger(__name__)

# Keys that describe the CONTENT of ``properties`` rather than a setting on the
# class. The generator replaces ``properties`` wholesale, so carrying these over
# pins the new schema to the old one's shape and each of them then fails
# somewhere else entirely:
#
#   required          -> validated per extracted object (extraction/validation.py),
#                        so a name no longer in the schema reports a missing
#                        required property on every document, forever.
#   $defs             -> the group/list-item definitions the OLD properties
#                        referenced; the new ones reference their own.
#   dependentRequired
#   propertyNames     -> same coupling, same failure shape.
#
# They are also not things an author sets *on the class* in the way a model pin or
# a threshold is, so preserving them was never the point. Named explicitly, since
# "preserve anything the generator did not emit" is otherwise the right default
# and this is the one place it needs a carve-out.
_PROPERTY_COUPLED_KEYS = frozenset(
    {"required", "$defs", "dependentRequired", "propertyNames"}
)

# Points at a property BY NAME, so it survives only if that property survived.
_INSTANCE_ARRAY_KEY = "x-aws-idp-instance-array"


def carry_forward_authored_settings(
    existing_class: Dict[str, Any],
    new_class: Dict[str, Any],
    synthesized: Collection[str] = (),
) -> List[str]:
    """Copy settings the generator did not produce from ``existing_class``.

    Mutates ``new_class`` in place and returns the keys carried forward.

    The rule is "preserve anything the generator did not emit", not a list of
    keys to keep: a deny-list would silently stop covering every extension key
    added after it was written. The generator owns ``properties`` and whatever
    else it actually produced.

    ``synthesized`` names keys the *caller* filled in itself rather than
    receiving from the model (Discovery derives ``description`` from a class id
    it had to rename). Those lose to an authored value, since they are not
    generator output either.

    Scope is deliberately class-level. Keys authored *inside* ``properties``
    (per-attribute ``x-aws-idp-evaluation-method`` / ``-threshold``) are still
    replaced, because a regenerated attribute can legitimately change type and
    carrying a stale evaluation method onto it could be worse than dropping it.

    Two carve-outs from the "anything not emitted" rule, both because the key
    describes the ``properties`` the generator just replaced rather than the
    class: ``_PROPERTY_COUPLED_KEYS`` are never carried, and
    ``x-aws-idp-instance-array`` is carried only while the property it names still
    exists — carrying it blindly produces a class that fails
    ``IDPConfig.validate_instance_array``, which would abort the whole save with a
    Pydantic error instead of losing one setting.
    """
    carried = [
        key
        for key in existing_class
        if (key not in new_class or key in synthesized)
        and key not in _PROPERTY_COUPLED_KEYS
        and not _drops_dangling_instance_array(key, existing_class, new_class)
    ]
    for key in carried:
        # Deep-copy: a carried list/dict would otherwise be shared with the
        # existing class dict, and `_apply_optimized_schema` hands its result back
        # to a caller that may still hold that dict.
        new_class[key] = copy.deepcopy(existing_class[key])
    if carried:
        logger.info(
            "Carried forward %d authored setting(s) onto regenerated class %r: %s",
            len(carried),
            new_class.get("$id"),
            ", ".join(sorted(carried)),
        )

    # Anything the generator did emit wins, but say so: an author who set one of
    # these needs the change visible here rather than in a later inference.
    # `description` is included because the discovery prompt asks the model for one
    # — so an authored description IS routinely replaced — and it is functional,
    # not decorative: the classification prompt's class table is built from it.
    overwritten = sorted(
        key
        for key, value in existing_class.items()
        if (key.startswith("x-aws-idp-") or key == "description")
        # The class id is rewritten by the CALLER (id normalization), not clobbered
        # by the generator, and `_normalize_class_id` already logs that rename.
        # Warning here as well would fire on every id repair.
        and key not in ("$id", "x-aws-idp-document-type")
        and key in new_class
        and key not in carried
        and new_class[key] != value
    )
    if overwritten:
        logger.warning(
            "Regenerated class %r replaced authored setting(s): %s",
            new_class.get("$id"),
            ", ".join(overwritten),
        )

    return carried


def _drops_dangling_instance_array(key, existing_class, new_class):
    """True when ``key`` is an instance-array pointer whose property is gone.

    ``x-aws-idp-instance-array`` names a top-level array property, and
    ``IDPConfig.validate_instance_array`` **raises** when that name is not in
    ``properties``. Carrying it onto a regenerated class whose properties no longer
    include it would turn a lost setting into an aborted save — every discovered
    class in the run lost, with an opaque Pydantic error. Shipped presets set it
    (``config_library/unified/ocr-benchmark``: ``checks``), so this is reachable.
    """
    if key != _INSTANCE_ARRAY_KEY:
        return False
    name = existing_class.get(_INSTANCE_ARRAY_KEY)
    properties = new_class.get("properties")
    if isinstance(properties, dict) and name in properties:
        return False
    logger.warning(
        "Not carrying %s=%r onto regenerated class %r: the regenerated schema has "
        "no such property, and keeping it would fail configuration validation. "
        "Re-declare the instance array on the new schema if the class still holds "
        "several records per section.",
        _INSTANCE_ARRAY_KEY,
        name,
        new_class.get("$id"),
    )
    return True
