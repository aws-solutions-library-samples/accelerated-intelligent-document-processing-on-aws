# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pricing a document's metering from the configuration's ``pricing`` section.

This is the one implementation of "metering row -> estimated dollars". The
reporting writer (``SaveReportingData.save_metering_data``) uses it for the
metering Parquet and the ``DocumentEstimatedCost`` metric, and the SDK's fit check
uses it for its per-document estimates, so the two figures cannot diverge.

It imports nothing heavier than the configuration models, so a caller that only
needs a cost (the SDK, a CLI) does not have to install pyarrow.

The rules themselves are documented on :func:`unit_cost`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, Mapping, Optional, Set, Tuple

from idp_common.config.models import read_price
from idp_common.metering_units import (
    CACHE_UNITS,
    NOT_CHARGEABLE,
    classify_absent_unit,
    pricing_key_service,
)

logger = logging.getLogger(__name__)

PricingMap = Dict[str, Dict[str, float]]


def build_pricing_map(pricing_entries: Optional[Iterable[Any]]) -> PricingMap:
    """Turn the configuration's ``pricing`` entries into ``{service: {unit: price}}``.

    ``pricing_entries`` is ``IDPConfig.pricing``: objects with ``name`` and ``units``,
    each unit with ``name`` and ``price``.

    An entry is recorded even when none of its prices is valid, so
    :func:`unit_cost` matches it and reports the unit unpriced. Leaving it out let
    the suffix walk go on to a shorter key and price the unit from a different
    entry, while the web UI's cost panel (``toPricingLookup``) keeps the entry and
    shows the unit unpriced.
    """
    pricing_map: PricingMap = {}
    try:
        if pricing_entries:
            entries = list(pricing_entries)
            logger.info(f"Found {len(entries)} pricing entries in configuration")

            config_loaded_count = 0
            for service in entries:
                service_name = service.name
                pricing_map.setdefault(service_name, {})
                for unit_info in service.units:
                    unit_name = unit_info.name
                    # The same rule the save path enforces (PricingConfig), so a
                    # price is priced here exactly when the Pricing page accepts
                    # it; ``float()`` alone also took nan, inf and ``1_000``, which
                    # the page reads differently.
                    price = read_price(unit_info.price)
                    if price is None:
                        logger.warning(
                            f"Invalid price value for {service_name}/{unit_name}: {unit_info.price!r}. Skipping entry."
                        )
                        continue
                    pricing_map[service_name][unit_name] = price
                    config_loaded_count += 1

            if config_loaded_count > 0:
                logger.info(
                    f"Successfully loaded {config_loaded_count} pricing entries from configuration"
                )
            else:
                logger.warning("No valid pricing data found in configuration")
        else:
            logger.warning("No pricing section found in configuration")

    except Exception as e:
        logger.error(f"Error processing pricing from configuration: {str(e)}")

    return pricing_map


def unit_cost(
    pricing_map: Mapping[str, Mapping[str, float]],
    service_api: str,
    unit: str,
    value: Any = 1,
) -> Optional[float]:
    """The unit cost for one service API and unit, from ``pricing_map``.

    Resolution is by EXACT pricing key. ``service_api`` is tried first, then
    progressively shorter ``/``-delimited suffixes of it, and the longest match
    wins — the same rule the benchmark harness uses
    (``benchmarks/harness/lib.py::price_metering``), so the two cost figures the
    project publishes cannot diverge. Every shipped pricing key is fully qualified
    (``bedrock/<model-id>``, ``textract/<api>``, ``lambda_hook/<function-name>``),
    so the suffix walk only ever strips leading context components; it cannot bind
    one model to another.

    The suffix walk is also what keeps a pricing key that is *less* qualified than
    the metering key working, which is why a user config that keys a Lambda hook on
    the bare ``GENAIIDP-<name>`` still resolves even though the shipped default is
    now ``lambda_hook/GENAIIDP-<name>``. Note the corollary: a Lambda hook's
    metering key must carry the bare function name, not the configured ARN — an ARN
    delimits the name with ``:``, which this walk cannot split, so an ARN-keyed
    metering row is unpriceable. See ``bedrock.client.lambda_hook_metering_name``.

    There is deliberately NO substring/fuzzy fallback. An earlier implementation
    accepted a pricing key that was merely a substring of the requested
    ``service_api`` (or vice versa), and a unit name that was merely a substring of
    the requested ``unit``. Because ``inputTokens`` is the first unit in every
    pricing row, ``cacheReadInputTokens`` bound to it and cache reads were billed at
    the FRESH INPUT rate: up to 10x their real price, always in the expensive
    direction. See GitHub issue #926.

    A unit the matched entry does not list is only $0.00 when
    ``metering_units.classify_absent_unit`` says so — the metered count is zero, or
    the unit is declared non-chargeable for that service on the authority of AWS's
    own price list. Anything else is ``None``: Bedrock's ``usage`` block grows
    numeric members and ``bedrock.client.numeric_usage`` forwards every one of them
    into metering, so an undeclared unit used to be a new billable dimension priced
    at exactly $0.00 with nothing to query for (GitHub issue #1212).

    ``value`` defaults to ``1`` rather than to ``0``, and the direction is the
    point: ``0`` would mean a caller that forgot to pass it got "free" for every
    absent unit — the check silently off, which is the state #1212 is about. ``1``
    means such a caller gets the strict answer instead, so the omission shows up as
    a NULL to investigate rather than as a zero nobody sees.

    Returns:
        The unit cost in USD; ``0.0`` when a pricing entry exists and either lists
        this unit or legitimately omits it; or ``None`` when no pricing entry exists
        for ``service_api`` at all, or when one exists and omits a unit that is
        neither zero nor declared free. ``None`` is an explicit "unpriced" result,
        not a price — a caller records it as NULL so a pricing gap stays
        distinguishable from something that is genuinely free. Never silently
        substitutes a related model's price.
    """
    parts = service_api.split("/")
    for start in range(len(parts)):
        candidate = "/".join(parts[start:])
        service_costs = pricing_map.get(candidate)
        if service_costs is None:
            continue
        if unit in service_costs:
            return service_costs[unit]
        # The entry exists but does not list this unit. Whether that means "not
        # chargeable" or "nobody has priced this yet" is the one question
        # classify_absent_unit answers, from a declaration sourced to AWS's
        # published price list rather than from the shape of pricing.yaml. Reading
        # the absence itself as "free" is what priced a newly added Bedrock usage
        # field at $0.00 (#1212).
        if classify_absent_unit(candidate, unit, value) == NOT_CHARGEABLE:
            logger.debug(
                f"Pricing entry '{candidate}' lists no unit '{unit}'; "
                f"not chargeable ($0.0)"
            )
            return 0.0
        declaration = (
            "idp_common.metering_units.EXPECTED_CACHE_UNIT_OMISSIONS, per "
            "pricing key and per unit"
            if unit in CACHE_UNITS
            else "idp_common.metering_units.NON_CHARGEABLE_METERING_UNITS, "
            f"for the '{pricing_key_service(candidate)}' service"
        )
        logger.warning(
            f"UNPRICED UNIT: pricing entry '{candidate}' lists no unit "
            f"'{unit}' and nothing declares it non-chargeable in "
            f"{declaration}. "
            f"Recording this metering row (value={value}) with a NULL cost "
            f"rather than $0.00, which would understate spend silently. "
            f"Either add a '{unit}' rate to the '{candidate}' pricing entry, "
            f"or declare the unit non-chargeable there with the AWS "
            f"price-list reading that shows it is."
        )
        return None

    logger.warning(
        f"UNPRICED: no pricing entry for service_api='{service_api}' "
        f"(unit='{unit}'). Recording this metering row with a NULL cost "
        f"rather than $0.00, which would understate spend silently. Add a "
        f"'{service_api}' entry to the pricing configuration."
    )
    return None


def iter_metering_rows(
    metering: Mapping[str, Mapping[str, Any]],
) -> Iterator[Tuple[str, str, str, float]]:
    """Yield ``(context, service_api, unit, value)`` for every metering row.

    A metering key is ``<context>/<service_api>``; a key with no ``/`` has an empty
    context and is the service API. A value that is not a number becomes ``1.0``
    with a warning, which is a non-zero the strict pricing branch sees rather than
    a string the absent-unit classifier would read as unreadable.
    """
    for key, metrics in metering.items():
        parts = key.split("/", 1)
        if len(parts) == 2:
            context, service_api = parts
        else:
            context = ""
            service_api = key
        for unit, value in metrics.items():
            try:
                float_value = float(value)
            except (ValueError, TypeError):
                float_value = 1.0
                logger.warning(
                    f"Could not convert metering value to float: {value}, using 1.0 instead"
                )
            yield context, service_api, unit, float_value


@dataclass
class MeteringCost:
    """A document's estimated cost, with what could not be priced.

    ``estimated_cost_usd`` is ``None`` when no row had a price at all, so a pricing
    gap is never reported as a measured $0.00; ``unpriced_row_count`` says how many
    rows the figure leaves out.
    """

    estimated_cost_usd: Optional[float] = None
    unpriced_row_count: int = 0
    unpriced_rows: Set[str] = field(default_factory=set)


def estimate_metering_cost(
    metering: Mapping[str, Mapping[str, Any]],
    pricing_map: Mapping[str, Mapping[str, float]],
) -> MeteringCost:
    """Sum ``value * unit_cost`` over a document's metering rows.

    The same arithmetic, in the same row order, as the per-row ``estimated_cost``
    the reporting writer stores, so this total equals the sum of that document's
    metering Parquet rows.
    """
    result = MeteringCost()
    for _context, service_api, unit, value in iter_metering_rows(metering):
        cost = unit_cost(pricing_map, service_api, unit, value)
        if cost is None:
            result.unpriced_rows.add(f"{service_api} [{unit}]")
            result.unpriced_row_count += 1
            continue
        row_cost = value * cost
        result.estimated_cost_usd = (
            row_cost
            if result.estimated_cost_usd is None
            else result.estimated_cost_usd + row_cost
        )
    return result
