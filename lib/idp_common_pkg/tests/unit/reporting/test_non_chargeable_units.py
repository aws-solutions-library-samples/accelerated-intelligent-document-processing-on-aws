# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The declared free-unit set, checked against the two things that can falsify it.

``idp_common.metering_units.NON_CHARGEABLE_METERING_UNITS`` is the declaration
GitHub issue #1212 asked for: which metering units a pricing entry may omit
without that meaning "unpriced". A declaration is only worth the sourcing behind
it, so this module asserts both halves:

* **the authority** — the Bedrock entries are asserted against a committed digest
  of AWS's own published price list, derived by
  ``bedrock_price_list_digest.py``. Offline by default (a blocking gate that
  needs egress red-lines the branch on somebody else's outage);
  ``CHECK_AWS_PRICE_LIST=1`` re-fetches and re-derives.
* **non-vacuity** — every declared entry must currently shield a unit that is
  really metered by one of this repository's writers and really absent from a
  shipped pricing entry. The metered-unit universe is read out of the writers'
  source rather than typed here, because a hand-written fixture would reproduce
  #1212's defect inside its own test.

And the capability the whole thing exists for: a numeric member Bedrock adds to
its ``usage`` block must reach the reporting table as NULL, not as $0.00, and it
must do so for *any* spelling rather than for the spellings somebody anticipated.
"""

from __future__ import annotations

import ast
import glob
import json
import os
import re
from typing import Any

import pytest
import yaml

from idp_common.metering_units import (
    AWS_BEDROCK_PRICE_LIST,
    NON_CHARGEABLE_METERING_UNITS,
    NOT_CHARGEABLE,
    UNKNOWN,
    classify_absent_unit,
    pricing_key_service,
)

from .bedrock_price_list_digest import (
    OFFER_URL,
    ON_DEMAND_INFERENCE,
    digest_offer_file,
    load_digest,
)

REPO = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
)

#: The code that builds a metering map, and therefore the only source a
#: declaration may draw a unit from. ``bedrock/client.py`` constructs both Bedrock
#: and LambdaHook metering entries; each shipped hook constructs the ``usage``
#: block the first of those spreads.
#:
#: ``utils/lambda_metering.py`` is deliberately absent: its dict keys are metering
#: *keys* built with f-strings (``f"{context}/lambda/requests"``), so the walk
#: below finds nothing in it at all and listing it would be an inert member —
#: which is the thing this module refuses to accept in the declaration itself.
#: ``bedrock/openai_responses.py`` is absent for the opposite reason, and it is
#: the more important one: it holds the *raw* OpenAI Responses field names
#: (``input_tokens``, ``input_tokens_details``, …) beside the camelCase usage
#: block it builds, and those are not metering units. Left in, they widened the
#: universe enough that a declared entry could be changed to
#: ``input_tokens_details`` — a field nothing in this repository meters — and the
#: non-vacuity assertion below stayed green. It is still watched, in
#: :data:`WATCHED_USAGE_READERS`, so its drift is visible; it just cannot supply
#: a declaration.
METERING_WRITERS = ("lib/idp_common_pkg/idp_common/bedrock/client.py",) + tuple(
    os.path.relpath(p, REPO)
    for p in sorted(
        glob.glob(os.path.join(REPO, "samples/lambda-hook-inference/*/index.py"))
    )
)

#: Read and pinned, but not a source for the declaration — see above.
WATCHED_USAGE_READERS = ("lib/idp_common_pkg/idp_common/bedrock/openai_responses.py",)

#: What the walk finds in each file today, pinned per file rather than bounded by
#: a length. A length bound let both narrowings in :class:`_UsageKeyCollector` be
#: deleted wholesale with the suite green, and it let a writer be dropped from
#: :data:`METERING_WRITERS` without anything noticing, because the remaining files
#: covered every declared unit between them. Equality per file makes each entry
#: load-bearing in both directions: a usage field added to a writer fails here and
#: has to be triaged against the declaration, which is exactly the event #1212 is
#: about.
EXPECTED_WRITER_UNITS = {
    "lib/idp_common_pkg/idp_common/bedrock/client.py": {
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
        "inputTokens",
        "outputTokens",
        "requests",
        "totalTokens",
    },
    "lib/idp_common_pkg/idp_common/bedrock/openai_responses.py": {
        "cacheReadInputTokens",
        "cacheWriteInputTokens",
        "inputTokens",
        "input_tokens",
        "input_tokens_details",
        "outputTokens",
        "output_tokens",
        "output_tokens_details",
        "requests",
        "totalTokens",
        "total_tokens",
    },
    "samples/lambda-hook-inference/GENAIIDP-bedrock-proxy/index.py": {
        "inputTokens",
        "outputTokens",
        "totalTokens",
    },
    "samples/lambda-hook-inference/GENAIIDP-chandra-ocr-hook/index.py": {
        "inputTokens",
        "outputTokens",
        "totalTokens",
    },
    "samples/lambda-hook-inference/GENAIIDP-cohere-parse-hook/index.py": {
        "inputTokens",
        "outputTokens",
        "pages",
        "totalTokens",
    },
    "samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook/index.py": {
        "inputTokens",
        "outputTokens",
        "pages",
        "totalTokens",
    },
    "samples/lambda-hook-inference/GENAIIDP-sagemaker-hook/index.py": {
        "inputTokens",
        "outputTokens",
        "totalTokens",
    },
    "samples/lambda-hook-inference/GENAIIDP-w2-copy-consistency/index.py": {
        "inputTokens",
        "outputTokens",
        "totalTokens",
    },
}

#: How many entries the declaration holds. Pinned, so a seventh free unit is a
#: deliberate edit here with its sourcing reviewed beside it, rather than a line
#: added to a list. This is the count ratchet named in
#: ``scripts/tests/gate_exemptions.json``.
DECLARED_ENTRY_COUNT = 6

#: How many shipped pricing entries each declaration currently shields — the
#: number of ``<service>/...`` rows in ``config_library/pricing.yaml`` that omit
#: that unit. Pinned because non-vacuity alone only asks for *one*: without this,
#: pricing ``totalTokens`` on 93 of the 94 Bedrock entries would leave the
#: shielded count at 1 with the suite green, and the declaration would be doing
#: almost nothing while still reading as a live decision.
EXPECTED_SHIELDED_SITES = {
    ("bedrock", "totalTokens"): 94,
    ("bedrock", "requests"): 94,
    ("bedrock", "cacheReadInputTokens"): 11,
    ("bedrock", "cacheWriteInputTokens"): 11,
    ("lambda_hook", "totalTokens"): 2,
    ("lambda_hook", "requests"): 2,
}


# --------------------------------------------------------------------------- #
# The metered-unit universe, read out of the writers rather than typed here     #
# --------------------------------------------------------------------------- #


class _UsageKeyCollector(ast.NodeVisitor):
    """String keys that a module treats as members of a ``usage`` block.

    Two shapes, both of which every metering writer here uses:

    * a dict literal bound to ``usage`` / ``metering`` (by assignment or as the
      value of a ``"usage"`` / ``"metering"`` key) — the whole of what a
      LambdaHook returns, and what ``openai_responses`` builds for the Responses
      API;
    * ``<something>["usage"].get("X")`` or ``usage.get("X")`` — how
      ``bedrock.client`` reads a Converse response.

    Deliberately narrow. Collecting every string constant in these files would
    make the non-vacuity assertion below pass for a unit nobody meters, which is
    the shape of vacuity this suite is guarding against elsewhere.
    """

    #: Names a module binds a metering-bound usage block to. ``usage_info`` is
    #: deliberately NOT here: in the mistral hook it holds the **Mistral API's**
    #: response, so reading it admitted ``pages_processed`` — a field of that
    #: response, which the hook never meters (it emits ``pages``) — into the set a
    #: declaration may draw from. That is the ``input_tokens_details`` hole one
    #: member along: declaring ``pages_processed`` free for ``lambda_hook`` passed
    #: non-vacuity. Dropping it costs nothing, because a hook's real units come
    #: from its ``"usage": {...}`` literal through :meth:`visit_Dict`; it also
    #: removes a false red, since a refactor reading that API field through a local
    #: failed the pin without changing any metering.
    USAGE_NAMES = frozenset({"usage", "metering", "final_usage"})

    def __init__(self) -> None:
        self.keys: set[str] = set()

    def _collect_dict(self, node: ast.AST) -> None:
        if not isinstance(node, ast.Dict):
            return
        for key in node.keys:
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                self.keys.add(key.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in self.USAGE_NAMES:
                self._collect_dict(node.value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if (
                isinstance(key, ast.Constant)
                and key.value in ("usage", "metering")
                and isinstance(value, ast.Dict)
            ):
                self._collect_dict(value)
                # A metering map is {key: {unit: count}} — one level deeper.
                for inner in value.values:
                    self._collect_dict(inner)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            receiver = func.value
            reads_usage = (
                isinstance(receiver, ast.Name) and receiver.id in self.USAGE_NAMES
            ) or (
                isinstance(receiver, ast.Subscript)
                and isinstance(receiver.slice, ast.Constant)
                and receiver.slice.value == "usage"
            )
            if reads_usage:
                self.keys.add(node.args[0].value)
        self.generic_visit(node)


def metered_units_from_source(
    paths: tuple[str, ...] = METERING_WRITERS,
) -> dict[str, frozenset[str]]:
    """``{relative path: unit names}`` read out of each named file."""
    found = {}
    for rel in paths:
        path = os.path.join(REPO, rel)
        collector = _UsageKeyCollector()
        collector.visit(ast.parse(open(path).read()))
        found[rel] = frozenset(collector.keys)
    return found


#: Stands in for a usage member whose value is computed rather than written out,
#: so no static claim can be made about it (``"pages": total_pages``). Named for
#: what it is rather than for what it is not: a ``NOT_A_`` prefix is in
#: ``scripts/exemption_discovery.py``'s name vocabulary, and this is not an
#: exemption of anything.
COMPUTED_VALUE = object()


def constant_usage_values(rel: str) -> dict[str, Any]:
    """``{unit: literal}`` for the usage members a file writes as a constant.

    A shipped per-page hook reports its token counts as the literal ``0``, and
    that literal is the whole reason those units are free on it. Reading it out of
    the hook is the difference between a test that measures the shipped shape and
    one that assumes it — the latter passes unchanged when the hook starts
    reporting real token counts against a per-page price, which is precisely the
    regression it was written to catch.
    """
    values: dict[str, Any] = {}

    class _Values(ast.NodeVisitor):
        def visit_Dict(self, node: ast.Dict) -> None:
            for key, value in zip(node.keys, node.values):
                if (
                    isinstance(key, ast.Constant)
                    and key.value in ("usage", "metering")
                    and isinstance(value, ast.Dict)
                ):
                    for k, v in zip(value.keys, value.values):
                        if isinstance(k, ast.Constant) and isinstance(k.value, str):
                            values[k.value] = (
                                v.value
                                if isinstance(v, ast.Constant)
                                else COMPUTED_VALUE
                            )
            self.generic_visit(node)

    _Values().visit(ast.parse(open(os.path.join(REPO, rel)).read()))
    return values


def all_metered_units() -> frozenset[str]:
    return frozenset().union(*metered_units_from_source().values())


def shipped_pricing_units() -> dict[str, frozenset[str]]:
    """``{pricing key: units it lists}`` from the real ``config_library``."""
    with open(os.path.join(REPO, "config_library", "pricing.yaml")) as fh:
        raw = yaml.safe_load(fh)
    return {
        entry["name"]: frozenset(u["name"] for u in entry.get("units") or [])
        for entry in raw["pricing"]
    }


@pytest.mark.unit
def test_the_source_derived_unit_universe_is_not_empty():
    """Without this the non-vacuity assertion below collects as a silent pass.

    A derivation that returns nothing makes every ``assert unit in universe``
    fail — which is loud — but the *parametrised* form of that assertion would
    collect zero cases and report as a skip, taking the guarantee with it. So the
    deriver is checked before it is used, in both directions: it must find the
    token units it is supposed to find, and it must not be collecting every
    string constant in the file (which it would if the narrowing above broke).
    """
    per_file = metered_units_from_source(METERING_WRITERS + WATCHED_USAGE_READERS)
    assert per_file, "no metering writers were read at all"
    assert set(per_file) == set(EXPECTED_WRITER_UNITS), (
        "the set of files read has changed; update EXPECTED_WRITER_UNITS in the "
        "same edit, because a file dropped from METERING_WRITERS narrows the "
        f"universe silently. Read: {sorted(per_file)}"
    )
    drift = {
        path: {"found": sorted(units), "pinned": sorted(EXPECTED_WRITER_UNITS[path])}
        for path, units in per_file.items()
        if units != frozenset(EXPECTED_WRITER_UNITS[path])
    }
    assert not drift, (
        "the usage members these files write have changed:\n"
        + json.dumps(drift, indent=2)
        + "\n\nThis is the event #1212 is about. For each new member: give it a "
        "rate in config_library/pricing.yaml, or declare it non-chargeable in "
        "idp_common.metering_units with the price-list reading that shows it is — "
        "then update the pin here. Do not update the pin alone."
    )
    universe = all_metered_units()
    for expected in ("inputTokens", "outputTokens", "totalTokens", "requests"):
        assert expected in universe, (
            f"the writer scan no longer finds {expected!r}, so every assertion "
            f"resting on it is vacuous. Found: {sorted(universe)}"
        )
    # The universe a declaration may draw on must exclude the raw OpenAI field
    # names: with them in it, a declared entry could name `input_tokens_details`
    # — a field nothing here meters — and non-vacuity stayed green.
    assert "input_tokens_details" not in universe, (
        "WATCHED_USAGE_READERS has leaked into the declaration universe; a "
        "declared unit could then name a field nothing meters"
    )


# --------------------------------------------------------------------------- #
# The declaration itself                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_declaration_is_not_empty_and_its_size_is_pinned():
    assert NON_CHARGEABLE_METERING_UNITS, (
        "the free-unit declaration is empty, which makes every parametrised "
        "assertion over it collect zero cases and report as a skip"
    )
    assert len(NON_CHARGEABLE_METERING_UNITS) == DECLARED_ENTRY_COUNT, (
        f"the declaration now holds {len(NON_CHARGEABLE_METERING_UNITS)} entries, "
        f"not {DECLARED_ENTRY_COUNT}. Declaring a unit free removes it from the "
        "unpriced report for every deployment, so update this pin in the same "
        "change that adds the entry — and put the price-list reading that "
        "establishes it in the entry's `basis`."
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry", NON_CHARGEABLE_METERING_UNITS, ids=lambda e: f"{e.service}:{e.unit}"
)
def test_every_entry_names_an_authority_and_says_what_it_showed(entry):
    """A URL or an explicit ``JUDGEMENT``, never an unattributed assertion."""
    assert entry.authority.startswith("https://") or entry.authority == "JUDGEMENT", (
        f"{entry.service}:{entry.unit} cites {entry.authority!r}, which is "
        "neither a URL nor the explicit JUDGEMENT marker"
    )
    assert len(entry.basis) > 80, (
        f"{entry.service}:{entry.unit} has no real basis text; a free unit needs "
        "what was read and what it showed, not a label"
    )
    assert "/" not in entry.service, (
        f"{entry.service!r} looks like a whole pricing key. The claim is about a "
        "service's billing dimensions, and scoping it to one model would make it "
        "a claim about the model instead"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "entry", NON_CHARGEABLE_METERING_UNITS, ids=lambda e: f"{e.service}:{e.unit}"
)
def test_every_entry_shields_a_unit_that_is_really_metered_and_really_unlisted(entry):
    """Non-vacuity, per member — the ratchet this declaration is registered with.

    An entry that shields nothing is not harmless: the lookup is by
    ``(service, unit)`` with no model and no line, so a dead entry pre-declares
    as free whatever next arrives under that name. Both halves are derived —
    metered from the writers' source, unlisted from the real
    ``config_library/pricing.yaml`` — so neither can be satisfied by belief.
    """
    universe = all_metered_units()
    assert entry.unit in universe, (
        f"{entry.unit!r} is declared non-chargeable but no metering writer in "
        f"{METERING_WRITERS} produces it, so the entry shields nothing and "
        f"pre-declares whatever next carries that name. Metered units: "
        f"{sorted(universe)}"
    )
    omitting = sorted(
        key
        for key, units in shipped_pricing_units().items()
        if pricing_key_service(key) == entry.service and entry.unit not in units
    )
    assert omitting, (
        f"every shipped '{entry.service}/...' pricing entry now lists "
        f"{entry.unit!r}, so this declaration shields nothing and should be "
        "deleted"
    )
    pinned = EXPECTED_SHIELDED_SITES[(entry.service, entry.unit)]
    assert len(omitting) == pinned, (
        f"{entry.service}:{entry.unit} shields {len(omitting)} shipped pricing "
        f"entries, not the {pinned} recorded in EXPECTED_SHIELDED_SITES. Fewer "
        "means rates have been added and the declaration may be on its way out; "
        "more means new entries arrived under its cover without being audited. "
        "Either way, read the change before moving the number."
    )


# --------------------------------------------------------------------------- #
# The authority: AWS's own published price list                                 #
# --------------------------------------------------------------------------- #


def _assert_price_list_supports_the_bedrock_entries(digest: dict[str, Any]) -> None:
    """The two Bedrock claims, asserted against a price-list digest.

    Shared by the offline test and the opt-in live one so that a refreshed
    price list is checked by exactly the assertions the committed one is.
    """
    assert digest["product_count"] > 1_000, (
        f"the digest describes {digest['product_count']} Bedrock products; that "
        "is not the offer file, and the absence assertions below would hold "
        "vacuously over it"
    )

    # totalTokens: no total-token billing dimension anywhere in Bedrock.
    assert digest["total_named_usagetypes"] == [], (
        "AWS now publishes a Bedrock usagetype naming a total token count: "
        f"{digest['total_named_usagetypes']}. The totalTokens declaration in "
        "idp_common.metering_units rests on there being none — re-read it before "
        "trusting any reported Bedrock cost."
    )
    assert digest["token_types"], "no tokenType values at all; digest is not usable"
    total_typed = [t for t in digest["token_types"] if re.search(r"total", t, re.I)]
    assert not total_typed, (
        f"Bedrock's tokenType attribute now takes {total_typed}, so a total token "
        "count may be a billing dimension; the totalTokens declaration needs "
        "re-deriving"
    )

    # requests: on-demand model inference is billed per token, never per call.
    assert digest["on_demand_inference_product_count"] > 1_000, (
        f"only {digest['on_demand_inference_product_count']} products carry "
        f"feature {ON_DEMAND_INFERENCE!r}; the unit assertion below would be "
        "over a sample too small to mean anything"
    )
    units = digest["on_demand_inference_units"]
    assert units, "no price dimensions found for on-demand inference"
    request_units = [u for u in units if re.search(r"request|api call", u, re.I)]
    assert not request_units, (
        f"on-demand Bedrock inference is now priced per request ({request_units}), "
        "so the 'requests' declaration in idp_common.metering_units understates "
        "every Bedrock cost this project reports. Add a per-request rate to the "
        "pricing entries and delete the declaration."
    )
    # The same claim's other half: Bedrock *does* charge per request elsewhere,
    # which is why the entry is scoped to a service rather than asserted of
    # Bedrock as a whole. If this were empty the scoping would look like
    # over-caution rather than the reason it is.
    assert digest["request_priced_features"], (
        "no request-priced Bedrock product found at all. The 'requests' entry's "
        "basis says the scoping matters because request pricing exists elsewhere "
        "in Bedrock; if that is no longer true, restate the basis."
    )


def _synthetic_offer(usagetype: str, unit: str, feature: str | None) -> dict[str, Any]:
    """One Bedrock-shaped product, in the offer file's own structure."""
    return {
        "version": "synthetic",
        "publicationDate": "2026-01-01T00:00:00Z",
        "products": {
            "SKU1": {
                "attributes": {
                    "usagetype": usagetype,
                    "tokenType": "Total Tokens"
                    if "total" in usagetype.lower()
                    else None,
                    **({"feature": feature} if feature else {}),
                }
            }
        },
        "terms": {
            "OnDemand": {
                "SKU1": {
                    "SKU1.offer": {
                        "priceDimensions": {
                            "d1": {"unit": unit, "description": "synthetic"}
                        }
                    }
                }
            }
        },
    }


@pytest.mark.unit
def test_the_deriver_would_report_a_total_token_dimension_if_one_existed():
    """A read-happened control for the assertion that carries the most weight.

    ``total_named_usagetypes == []`` is the whole basis of the ``totalTokens``
    declaration, and an empty list is also what a **broken** deriver returns — one
    reading ``usageType`` instead of ``usagetype``, say. The live check is opt-in
    and neither CI runs it, so without a positive control the load-bearing
    assertion could never fail for the reason it is written for. This runs offline
    against a synthetic offer file in the real structure.
    """
    found = digest_offer_file(
        _synthetic_offer("USE1-Claude-total-tokens", "1K tokens", ON_DEMAND_INFERENCE)
    )
    assert found["total_named_usagetypes"] == ["USE1-Claude-total-tokens"], (
        "the deriver does not see a total-token usagetype that is right in front "
        "of it, so its empty answer on the real offer file proves nothing"
    )
    assert any("total" in t.lower() for t in found["token_types"]), (
        "the deriver does not see a total-named tokenType either"
    )


@pytest.mark.unit
def test_the_deriver_would_report_a_request_priced_inference_product():
    """The same control for the other Bedrock claim.

    ``on_demand_inference_units`` containing no request unit is the basis of the
    ``requests`` declaration, and a deriver that matched the ``feature`` attribute
    wrongly would report the same empty answer for every input.
    """
    found = digest_offer_file(
        _synthetic_offer("USE1-Claude-invocations", "Requests", ON_DEMAND_INFERENCE)
    )
    assert found["on_demand_inference_product_count"] == 1, (
        "the deriver does not recognise its own On-demand Inference feature value"
    )
    assert found["on_demand_inference_units"] == {"Requests": 1}, (
        "the deriver does not report a request-priced on-demand inference "
        f"product: {found['on_demand_inference_units']}"
    )
    # And it must not claim an on-demand product where the feature says otherwise,
    # or the count above would be satisfied by everything.
    other = digest_offer_file(
        _synthetic_offer("USE1-Claude-invocations", "Requests", "Batch Inference")
    )
    assert other["on_demand_inference_product_count"] == 0
    assert other["on_demand_inference_units"] == {}


@pytest.mark.unit
def test_the_committed_price_list_digest_supports_the_declaration():
    digest = load_digest()
    assert digest["source"] == OFFER_URL == AWS_BEDROCK_PRICE_LIST, (
        "the digest, its deriver and the module's cited URL disagree about which "
        "price list was read"
    )
    _assert_price_list_supports_the_bedrock_entries(digest)


@pytest.mark.unit
@pytest.mark.skipif(
    os.environ.get("CHECK_AWS_PRICE_LIST") != "1",
    reason="needs network egress; set CHECK_AWS_PRICE_LIST=1 to re-derive live",
)
def test_the_live_price_list_still_supports_the_declaration():
    """Re-derive from AWS today rather than from the committed digest.

    Opt-in and not a gate, for the reason ``check_markdown_links.py`` never
    fetches an ``http`` URL: a blocking check that needs egress red-lines the
    branch on somebody else's outage. Run it when touching the declaration.
    """
    import urllib.request

    with urllib.request.urlopen(OFFER_URL, timeout=300) as fh:  # noqa: S310
        offer = json.load(fh)
    _assert_price_list_supports_the_bedrock_entries(digest_offer_file(offer))


# --------------------------------------------------------------------------- #
# The rule: what happens to a unit nothing declares                            #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
@pytest.mark.parametrize(
    "unit",
    [
        # A reasoning-token count, the shape most likely to arrive next.
        "reasoningTokens",
        # Anthropic's own name for the write side, which Bedrock does not use
        # today — a rename of an existing chargeable dimension.
        "cacheCreationInputTokens",
        # Case and separator variants of a unit that IS declared. The lookup is
        # exact, so these must NOT inherit totalTokens' declaration: an exact
        # lookup that quietly matched them would be the #926 substring defect
        # coming back on the unit axis.
        "TotalTokens",
        "totaltokens",
        "total_tokens",
        # A per-call dimension under a name the declaration does not hold.
        "requestCount",
        "invocations",
        # A structured member that became numeric.
        "cacheDetails",
    ],
)
def test_an_undeclared_unit_with_a_real_count_is_unknown_not_free(unit):
    """The capability, probed with several spellings of the thing it must catch.

    The rule is *whether the unit is declared for this service*, not whether it
    resembles something. A guard written as a pattern over plausible new names
    would pass this list and miss the next one.
    """
    verdict = classify_absent_unit("bedrock/us.anthropic.claude-toy-v1:0", unit, 5_000)
    assert verdict == UNKNOWN, (
        f"{unit!r} is not declared non-chargeable for 'bedrock' yet resolves to "
        f"{verdict!r}; 5000 of it would be billed at $0.00"
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "unit,service",
    [(e.unit, e.service) for e in NON_CHARGEABLE_METERING_UNITS],
)
def test_a_declared_unit_is_free_even_with_a_large_count(unit, service):
    assert (
        classify_absent_unit(f"{service}/anything", unit, 10_000_000) == NOT_CHARGEABLE
    )


@pytest.mark.unit
def test_a_declaration_does_not_leak_across_services():
    """``requests`` is free for Bedrock inference and a real AWS Lambda charge.

    One entry covering both services would be false for one of them, which is
    the defect class the exemption registry exists for: one justification
    attached to a set whose members do not share it.
    """
    assert classify_absent_unit("bedrock/m", "requests", 1) == NOT_CHARGEABLE
    assert classify_absent_unit("lambda/requests", "requests", 1) == UNKNOWN
    assert classify_absent_unit("textract/analyze_document", "requests", 1) == UNKNOWN


@pytest.mark.unit
@pytest.mark.parametrize("zero", [0, 0.0, -0.0])
def test_a_zero_count_is_free_whatever_the_unit_is(zero):
    """Arithmetic, not a declaration: cost is count x rate.

    This is what keeps the shipped per-page OCR hooks quiet — they report
    ``inputTokens``/``outputTokens``/``totalTokens`` as 0 against a price listing
    only ``pages`` — without any of those units having to be declared free for
    LambdaHooks in general.
    """
    assert classify_absent_unit("lambda_hook/anything", "inputTokens", zero) == (
        NOT_CHARGEABLE
    )
    assert (
        classify_absent_unit("bedrock/m", "somethingBrandNew", zero) == NOT_CHARGEABLE
    )


@pytest.mark.unit
def test_an_unreadable_count_is_not_treated_as_a_measured_zero():
    """A count that is not a number is UNKNOWN, not free.

    ``None`` from a failed decode and ``True`` from a DynamoDB ``BOOL`` are the
    two live shapes. Reading either as zero is how a failed read becomes a value.
    """
    for bad in (None, True, False, "0", [], {}):
        assert classify_absent_unit("bedrock/m", "mysteryTokens", bad) == UNKNOWN, (
            f"a count of {bad!r} resolved as a free unit"
        )


@pytest.mark.unit
def test_the_shipped_lambda_hook_usage_shape_produces_no_unpriced_units():
    """The regression this change most plausibly causes, measured on real data.

    A shipped per-page OCR hook's ``usage`` block is read out of its own source
    and every unit in it is classified against the shipped per-page pricing
    entry. If any came back UNKNOWN, every deployment using that hook would
    start writing NULL costs.
    """
    hook = os.path.join(
        REPO, "samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook/index.py"
    )
    collector = _UsageKeyCollector()
    collector.visit(ast.parse(open(hook).read()))
    units = collector.keys & {
        "pages",
        "inputTokens",
        "outputTokens",
        "totalTokens",
    }
    assert units == {"pages", "inputTokens", "outputTokens", "totalTokens"}, (
        f"the mistral OCR hook's usage block scanned as {sorted(units)}; this "
        "test is no longer about the shipped shape"
    )
    priced = shipped_pricing_units()
    key = "lambda_hook/GENAIIDP-mistral-ocr-hook"
    assert priced.get(key) == frozenset({"pages"}), (
        f"expected the shipped mistral hook to be priced per page only, got "
        f"{priced.get(key)}"
    )

    # The COUNTS come out of the hook too, not from a literal written here. That
    # is the difference between measuring the shipped shape and assuming it: with
    # a hardcoded 0 this test hit the zero-count branch, which is free for any
    # unit whatsoever, so changing the hook to report real token counts against
    # its per-page price left it green while every page processed would write a
    # NULL cost — the exact regression the docstring above claims to catch.
    values = constant_usage_values(
        "samples/lambda-hook-inference/GENAIIDP-mistral-ocr-hook/index.py"
    )
    token_units = sorted(units - {"pages"})
    assert token_units, "no token units found in the hook's usage block"
    for unit in token_units:
        assert values.get(unit) == 0, (
            f"the shipped mistral hook now reports a non-literal-zero {unit!r} "
            f"({values.get(unit)!r}) against a pricing entry that lists only "
            f"'pages'. That unit is NOT declared non-chargeable for "
            f"'lambda_hook', so every row carrying it will be written with a "
            f"NULL cost. Either give the hook's pricing entry a rate for it, or "
            f"declare it with the reading that shows it is free."
        )
        assert classify_absent_unit(key, unit, values[unit]) == NOT_CHARGEABLE

    # And the discriminating half: those units are free *because* the count is
    # zero, not because anything declares them free for a LambdaHook. Without
    # this the assertion above would pass for a rule that declared every unit
    # free on every service.
    for unit in ("inputTokens", "outputTokens"):
        assert classify_absent_unit(key, unit, 1_234) == UNKNOWN, (
            f"{unit!r} is treated as free for a lambda_hook even with a real "
            "count; the zero-count branch is not what is doing the work here"
        )

    # 'requests' is injected as 1 by BedrockClient, so it is non-zero and has to
    # be declared rather than falling to the arithmetic branch.
    assert classify_absent_unit(key, "requests", 1) == NOT_CHARGEABLE


# --------------------------------------------------------------------------- #
# End to end: what the reporting table actually gets                           #
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_the_classifier_judges_the_number_the_row_records():
    """A count of ``"0"`` is a measured zero, and the row stores it as ``0.0``.

    ``save_metering_data`` converts every count to a float before writing it, so
    classifying on the raw value judged one number while storing another: a count
    arriving as a string — which a DynamoDB ``N`` attribute decoded by the wrong
    reader does — read as unreadable and produced a NULL cost for a row whose
    recorded value is ``0.0``. Passing ``float_value`` is what makes the two
    agree, and this is the input that tells them apart.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch

    from idp_common.config.models import IDPConfig
    from idp_common.models import Document
    from idp_common.reporting import SaveReportingData

    reporter = SaveReportingData(
        "test-bucket",
        config=IDPConfig.model_validate(
            {
                "pricing": [
                    {
                        "name": "bedrock/toy-model",
                        "units": [{"name": "inputTokens", "price": "1.0E-6"}],
                    }
                ]
            }
        ),
    )
    document = Document(
        id="doc-strcount",
        input_key="doc.pdf",
        num_pages=1,
        initial_event_time=datetime.now(timezone.utc).isoformat(),
        metering={"Extraction/bedrock/toy-model": {"mysteryTokens": "0"}},
    )
    with patch.object(reporter, "_save_records_as_parquet") as mock_save:
        reporter.save_metering_data(document)
    row = mock_save.call_args.args[0][0]

    assert row["value"] == 0.0
    assert row["unit_cost"] == 0.0, (
        "the row records 0.0 but the unit was classified on the raw '0' string "
        "and came back unpriced; the classifier is judging a different number "
        "from the one stored"
    )
    assert row["estimated_cost"] == 0.0


@pytest.mark.unit
def test_a_new_bedrock_usage_field_reaches_the_table_as_null_not_zero():
    """The defect in #1212's title, end to end through ``save_metering_data``.

    The unit name here is not invented: it is put through
    ``bedrock.client.numeric_usage`` exactly as a live Converse response would
    be, which is the code that forwards *every* numeric member of a ``usage``
    block into metering and is therefore the reason an unanticipated field
    arrives at all. The structured member beside it is carried through the same
    call to show it is still dropped rather than reported.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch

    from idp_common.bedrock.client import numeric_usage
    from idp_common.config.models import IDPConfig
    from idp_common.models import Document
    from idp_common.reporting import SaveReportingData

    # A Converse usage block as Bedrock would return it, one release from now.
    usage = {
        "inputTokens": 1_000,
        "outputTokens": 200,
        "totalTokens": 1_200,
        "reasoningTokens": 5_000,
        "cacheDetails": [{"ttl": "5m", "inputTokens": 900}],
    }
    metered = numeric_usage(usage)
    assert "reasoningTokens" in metered, (
        "numeric_usage no longer forwards unknown numeric members, so this test "
        "is not measuring the path #1212 is about"
    )
    assert "cacheDetails" not in metered

    reporter = SaveReportingData(
        "test-bucket",
        config=IDPConfig.model_validate(
            {
                "pricing": [
                    {
                        "name": "bedrock/toy-model",
                        "units": [
                            {"name": "inputTokens", "price": "1.0E-6"},
                            {"name": "outputTokens", "price": "5.0E-6"},
                        ],
                    }
                ]
            }
        ),
    )
    document = Document(
        id="doc-1212",
        input_key="doc.pdf",
        num_pages=1,
        initial_event_time=datetime.now(timezone.utc).isoformat(),
        metering={"Extraction/bedrock/toy-model": {**metered, "requests": 1}},
    )

    with patch.object(reporter, "_save_records_as_parquet") as mock_save:
        reporter.save_metering_data(document)
    rows = {r["unit"]: r for r in mock_save.call_args.args[0]}

    # The declared free units stay $0.00 — no reported cost changes.
    for free in ("totalTokens", "requests"):
        assert rows[free]["unit_cost"] == 0.0, f"{free} should still be free"
        assert rows[free]["estimated_cost"] == 0.0

    # The priced ones are untouched.
    assert rows["inputTokens"]["estimated_cost"] == pytest.approx(0.001)
    assert rows["outputTokens"]["estimated_cost"] == pytest.approx(0.001)

    # And the new one is NULL, so `WHERE unit_cost IS NULL` finds it. Before this
    # change it was 0.0, indistinguishable from a unit AWS does not charge for.
    new_field = rows["reasoningTokens"]
    assert new_field["unit_cost"] is None
    assert new_field["estimated_cost"] is None
    # The count itself is still recorded: the metering data is not lost, only the
    # claim that it cost nothing.
    assert new_field["value"] == 5_000.0
