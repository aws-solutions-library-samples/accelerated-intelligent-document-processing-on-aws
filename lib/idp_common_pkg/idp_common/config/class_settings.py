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

import logging
from typing import Any, Collection, Dict, List

logger = logging.getLogger(__name__)


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
    """
    carried = [
        key for key in existing_class if key not in new_class or key in synthesized
    ]
    for key in carried:
        new_class[key] = existing_class[key]
    if carried:
        logger.info(
            "Carried forward %d authored setting(s) onto regenerated class %r: %s",
            len(carried),
            new_class.get("$id"),
            ", ".join(sorted(carried)),
        )

    # Anything the generator did emit wins, but say so for extension keys: an
    # author who set one needs the change to be visible here rather than in a
    # later inference.
    overwritten = sorted(
        key
        for key, value in existing_class.items()
        if key.startswith("x-aws-idp-")
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
