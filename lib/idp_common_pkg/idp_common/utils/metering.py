# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Metering-map helpers: merging usage maps and a document's cumulative usage.

Stdlib only, so Lambda suites that stub ``idp_common`` can load this module by
path (see ``src/lambda/queue_processor/conftest.py``). Both functions are
re-exported from ``idp_common.utils``.
"""

import copy
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


def merge_metering_data(
    existing_metering: Dict[str, Any], new_metering: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Merge metering data from multiple sources

    Args:
        existing_metering: Existing metering data to merge into
        new_metering: New metering data to add

    Returns:
        Merged metering data
    """
    merged = existing_metering.copy()

    for service_api, metrics in new_metering.items():
        if isinstance(metrics, dict):
            for unit, value in metrics.items():
                if service_api not in merged:
                    merged[service_api] = {}

                # Convert both values to numbers to handle string vs int mismatch
                try:
                    existing_value = merged[service_api].get(unit, 0)
                    # Handle both string and numeric values
                    if isinstance(existing_value, str):
                        existing_value = float(existing_value)
                    if isinstance(value, str):
                        value = float(value)

                    merged[service_api][unit] = existing_value + value
                except (ValueError, TypeError) as e:
                    logger.warning(
                        f"Error converting metering values for {service_api}.{unit}: existing={merged[service_api].get(unit)}, new={value}, error={e}"
                    )
                    # Fallback to new value if conversion fails
                    merged[service_api][unit] = value
        else:
            logger.warning(
                f"Unexpected metering data format for {service_api}: {metrics}"
            )

    return merged


def cumulative_metering(doc: Any) -> Dict[str, Any]:
    """
    Return a document's cumulative usage: earlier runs' baseline plus this run's own.

    ``Document.metering`` holds only the current run's usage; the usage of earlier
    runs is carried in ``Document.metering_baseline`` (set at run start). The
    tracking-table ``Metering`` attribute and the review hold snapshot store this
    cumulative total, while reporting and the per-run record use ``metering`` only.

    Both inputs are deep-copied first because ``merge_metering_data`` makes only a
    shallow copy and mutates the inner dicts. Accepts any object with a
    ``metering`` attribute; a missing ``metering_baseline`` counts as empty.

    Args:
        doc: A ``Document`` (or any object with ``metering`` and optionally
            ``metering_baseline`` attributes)

    Returns:
        A new dict with baseline and own usage summed per service/unit
    """
    baseline = copy.deepcopy(getattr(doc, "metering_baseline", {}) or {})
    own = copy.deepcopy(getattr(doc, "metering", {}) or {})
    return merge_metering_data(baseline, own)


#: Attribute an exception carries when the work that raised it had already made
#: billed model calls. Set by :func:`attach_failure_metering`, read by
#: :func:`failure_metering`.
_FAILURE_METERING_ATTR = "idp_failure_metering"


def attach_failure_metering(
    error: BaseException, metering: Dict[str, Any]
) -> BaseException:
    """Record on ``error`` the usage of calls made before it was raised.

    A failure that raises has no return value to carry its metering in, so the
    usage of every model call that completed before it would otherwise be billed
    and never reported. The exception keeps its type, message and traceback —
    Step Functions retries by class name, and callers match on the type — and
    gains only this attribute. An empty ``metering`` attaches nothing.

    Calling it twice on one exception replaces the earlier value rather than adding
    to it, because the one producer (agentic ``structured_output_async``) records
    the agent's cumulative usage, and summing two cumulative readings would count
    the earlier calls twice.

    Returns ``error`` so a caller can write ``raise attach_failure_metering(e, m)``.
    """
    if metering:
        setattr(error, _FAILURE_METERING_ATTR, copy.deepcopy(metering))
    return error


def failure_metering(error: BaseException | None) -> Dict[str, Any]:
    """Return a copy of the metering :func:`attach_failure_metering` put on ``error``.

    ``{}`` when there is none. Only ``error`` itself is read, never its
    ``__cause__`` or ``__context__``: a caller that wraps the exception is expected
    to read it before wrapping, and following the chain could pick up the usage of
    a failure that an inner handler already merged and recovered from.
    """
    metering = getattr(error, _FAILURE_METERING_ATTR, None)
    return copy.deepcopy(metering) if isinstance(metering, dict) else {}
