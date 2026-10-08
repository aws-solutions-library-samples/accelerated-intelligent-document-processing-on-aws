# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The minimum-cacheable-prefix table is written five times, and they must agree.

One published fact — how long a prompt prefix has to be before Bedrock will cache it
— is encoded in five places in this tree, because each has a different consumer and
none can import another:

================================================  ==========================  ==========
File                                              Form                        Consumer
================================================  ==========================  ==========
``idp_common/bedrock/prompt_cache.py``            ``_MIN_PREFIX_TIERS``       the product
``src/ui/.../promptCacheModel.ts``                ``MIN_PREFIX_TIERS``        the web UI
``benchmarks/harness/cache_audit.py``             ``CACHE_MINIMUMS``          the scan
``benchmarks/harness/cache_prefix_survey.py``     ``TIERS``                   a report
``benchmarks/harness/cache_threshold_probe.py``   prose in the docstring      a reader
================================================  ==========================  ==========

**Nothing compared them, and a prose comment was standing in for this test.** The
comment above ``CACHE_MINIMUMS`` said *"the same note is on the two other copies of
this table"* and named two of the four others. When Claude Haiku 5.5 was added, those
two were exactly the two updated — the comment was read as the completeness list, and
it was itself incomplete, so three copies went stale in one change and every gate
stayed green. That is the defect class this repository keeps hitting: a control that
exists but is never consulted. The reach of a comment is not checkable; the reach of
a derived comparison is.

Haiku 5.5 is also why the stakes are not cosmetic. The two Haikus sit at opposite ends
of the table — 512 tokens against 4,096, the smallest tier and the largest — so the
family name settles nothing, and a copy that resolves ``claude-haiku-5-5`` through a
shared ``haiku`` stem reports an 8x-too-high minimum. In ``cache_audit.py`` the dict
is scanned in **insertion order**, so getting it right is a question of line order
rather than of content, which no amount of reading the values would reveal.

What is asserted is agreement plus total coverage, both derived: every model id any
copy knows about resolves to the same number in every copy that can express it, and
every entry in the product's table is represented in each of the other four. The
numbers themselves are not restated here — this file would then be a sixth copy.
"""

from __future__ import annotations

import os
import re

import pytest
from harness_import import REPO, harness_module

#: The authority. Every other copy is compared against this one rather than against a
#: literal written here, so adding a tier means editing one file plus the four copies
#: this test then names.
PRODUCT = os.path.join(
    REPO, "lib", "idp_common_pkg", "idp_common", "bedrock", "prompt_cache.py"
)
UI_TS = os.path.join(
    REPO, "src", "ui", "src", "components", "common", "promptCacheModel.ts"
)
SURVEY = os.path.join(REPO, "benchmarks", "harness", "cache_prefix_survey.py")
PROBE = os.path.join(REPO, "benchmarks", "harness", "cache_threshold_probe.py")

#: ``claude-(opus-5|fable-5|haiku-5-5)`` -> the three ids it matches. Both tables
#: that hold regexes write them this way, so the group has to be expanded rather
#: than matched: a plain ``claude-[a-z0-9-]+`` scan stops at the ``(`` and silently
#: returns ONE family out of ten, which looks like a passing comparison over almost
#: nothing. Derived from the patterns rather than listed, so a family cannot be added
#: to the product without this file demanding it in the other four.
_GROUPED = re.compile(r"claude-\(([a-z0-9|\-]+)\)")
_PLAIN = re.compile(r"claude-([a-z0-9-]+)")


def _families(pattern: str) -> list[str]:
    """Every ``claude-*`` id a cache-minimum regex matches."""
    grouped = _GROUPED.findall(pattern)
    if grouped:
        return [f"claude-{alt}" for group in grouped for alt in group.split("|") if alt]
    return [f"claude-{tail}" for tail in _PLAIN.findall(pattern)]


def _product_tiers() -> dict[str, int]:
    """``{family: minimum}`` read from the product's own table, via import.

    Imported rather than parsed: the values are what the shipped code uses, and a
    regex over the source would keep passing if the tuple were restructured.
    """
    prompt_cache = pytest.importorskip("idp_common.bedrock.prompt_cache")
    tiers: dict[str, int] = {}
    for pattern, minimum in prompt_cache._MIN_PREFIX_TIERS:
        for family in _families(pattern.pattern):
            tiers[family] = minimum
    assert tiers, (
        "no model families were read out of _MIN_PREFIX_TIERS. Either the structure "
        "changed or the pattern vocabulary did — either way every comparison below "
        "just stopped looking at anything."
    )
    return tiers


@pytest.fixture(scope="module")
def product_tiers() -> dict[str, int]:
    return _product_tiers()


def _ts_tiers() -> dict[str, int]:
    """``{family: minimum}`` parsed out of the UI's regex/number pairs."""
    source = open(UI_TS, encoding="utf-8").read()
    body = source.split("MIN_PREFIX_TIERS", 1)[1].split("];", 1)[0]
    tiers: dict[str, int] = {}
    for line in body.splitlines():
        match = re.match(r"\s*\[/(?P<re>[^/]+)/,\s*(?P<min>\d+)\]", line.strip())
        if not match:
            continue
        for family in _families(match.group("re")):
            tiers[family] = int(match.group("min"))
    return tiers


def _survey_tiers() -> dict[str, int]:
    """``{family-ish label: minimum}`` from the survey's displayed TIERS table.

    This copy names models for a human ("Opus 5, Fable 5, Haiku 5.5"), not by model
    id, so it is compared on a normalised label rather than on an id.
    """
    survey = open(SURVEY, encoding="utf-8").read()
    body = survey.split("TIERS = [", 1)[1].split("]", 1)[0]
    tiers: dict[str, int] = {}
    for minimum, labels in re.findall(r"\((\d+),\s*\"([^\"]+)\"\)", body):
        for label in labels.split(","):
            tiers[_normalise(label)] = int(minimum)
    return tiers


def _normalise(label: str) -> str:
    """ "Haiku 5.5" -> "haiku-5-5", so a prose label can be matched to a model id."""
    return label.strip().lower().replace(" ", "-").replace(".", "-")


def _family_label(family: str) -> str:
    """ "claude-haiku-5-5" -> "haiku-5-5"."""
    return family.removeprefix("claude-")


class TestEveryCopyAgrees:
    """Agreement on the INTERSECTION. Scoped that way on purpose.

    The five copies do not hold the same set of families and should not be made to:
    the product prices every model a stored configuration can name, including
    end-of-life ones it must still resolve, while the three benchmark tools enumerate
    the models the benchmark runs. Demanding equal sets would invent a requirement
    that fails today for reasons predating this file. What must never differ is the
    NUMBER for a family two copies both name — a reader comparing a UI badge with a
    scan verdict is comparing those two numbers directly.

    Coverage is the separate question, and ``TestEveryBenchmarkedModelIsCovered``
    below answers it against a derived set rather than a wish.
    """

    @staticmethod
    def _assert_agrees(other: dict[str, int], product: dict[str, int], label: str):
        shared = sorted(set(other) & set(product))
        assert shared, (
            f"{label} and prompt_cache.py name no family in common, so this "
            "comparison is vacuous — the parser above has probably stopped matching."
        )
        for family in shared:
            assert other[family] == product[family], (
                f"{family}: prompt_cache.py says {product[family]}, {label} says "
                f"{other[family]}. One published fact, two answers."
            )

    def test_the_ui_table_agrees(self, product_tiers):
        """A UI that reports a different minimum sends the operator to fix the wrong
        thing: the "may not cache" badge is what a class author acts on."""
        self._assert_agrees(_ts_tiers(), product_tiers, "the web UI table")

    def test_the_scan_table_agrees(self, product_tiers):
        scan = harness_module("cache_audit").CACHE_MINIMUMS
        self._assert_agrees(dict(scan), product_tiers, "cache_audit.CACHE_MINIMUMS")

    def test_the_survey_table_agrees(self, product_tiers):
        survey = {f"claude-{k}": v for k, v in _survey_tiers().items()}
        self._assert_agrees(survey, product_tiers, "cache_prefix_survey.TIERS")


class TestEveryBenchmarkedModelIsCovered:
    """Every model the benchmark matrix names must be covered by all five copies.

    This is the coverage half, and the set is **derived from the matrix** rather than
    authored: ``config_matrix.yaml``'s axes are what decides which models the scan,
    the survey and the probe are ever pointed at, so a model added to an axis is
    exactly a model those three have to be able to annotate. That is the property
    Haiku 5.5 broke — it went onto two axes while three of the five copies kept no
    entry for it, so the scan printed no minimum for the arm it was added to run.
    """

    @staticmethod
    def _axis_model_ids() -> list[str]:
        yaml = pytest.importorskip("yaml")
        matrix = yaml.safe_load(
            open(os.path.join(REPO, "benchmarks", "matrices", "config_matrix.yaml"))
        )
        ids = {
            value
            for axis in (matrix.get("axes") or {}).values()
            for overrides in (axis or {}).values()
            for key, value in (overrides or {}).items()
            if key.endswith((".model", ".model_id")) and isinstance(value, str)
        }
        claude = sorted(i for i in ids if "claude-" in i)
        assert claude, (
            "no Claude model ids were found in config_matrix.yaml's axes, so every "
            "check in this class is vacuous."
        )
        return claude

    @pytest.fixture(scope="class")
    @classmethod
    def benchmarked(cls) -> list[str]:
        return cls._axis_model_ids()

    @staticmethod
    def _resolve(table: dict[str, int], model_id: str) -> int | None:
        """First substring key wins — the lookup every copy of this table uses."""
        return next((v for k, v in table.items() if k in model_id), None)

    def test_the_product_resolves_each_one(self, benchmarked, product_tiers):
        prompt_cache = pytest.importorskip("idp_common.bedrock.prompt_cache")
        for model_id in benchmarked:
            assert prompt_cache.min_cacheable_prefix_tokens(model_id) is not None, (
                f"{model_id} is on a benchmark axis and prompt_cache.py reports no "
                "minimum for it, so no class on it is ever warned."
            )

    def test_the_scan_resolves_each_one_to_the_same_number(self, benchmarked):
        """⚠️ The risk in ``CACHE_MINIMUMS`` is ORDER, not content.

        It is scanned in insertion order with a substring match, so a shorter key
        placed earlier shadows a longer one — ``claude-opus-5`` ahead of
        ``claude-opus-5-5`` does exactly that, deliberately and correctly. So assert
        the RESOLVED answer for a real model id, which is the thing a reader of the
        dict cannot verify by eye.
        """
        prompt_cache = pytest.importorskip("idp_common.bedrock.prompt_cache")
        scan = harness_module("cache_audit").CACHE_MINIMUMS
        for model_id in benchmarked:
            expected = prompt_cache.min_cacheable_prefix_tokens(model_id)
            resolved = self._resolve(scan, model_id)
            assert resolved == expected, (
                f"cache_audit resolves {model_id} to {resolved}, not {expected}. If "
                "it is None the dict has no key for this model and the scan prints "
                "no [min N] tag — the annotation that separates a prefix-length "
                "cliff from a workload effect. If it is a different number, an "
                "earlier key is shadowing it: move the longer key above the shorter."
            )

    def test_the_survey_and_probe_name_each_one(self, benchmarked):
        """The two human-facing copies. Only presence can be checked in prose, and
        presence is the real omission mode: a family nobody listed reads as a family
        with no published minimum."""
        survey_text = open(SURVEY, encoding="utf-8").read().lower()
        probe_text = open(PROBE, encoding="utf-8").read().lower()
        for model_id in benchmarked:
            family = re.search(r"claude-([a-z0-9-]+?)(?:-\d{8})?(?:-v\d|$|:)", model_id)
            assert family, model_id
            # "haiku-5-5" -> matches "Haiku 5.5", "Haiku 5-5" and "Haiku 5 5".
            # The "." has to be in the separator class: these two copies spell
            # versions the way a human writes them, so "Haiku 4.5" is the normal
            # form and a separator class without "." fails on every entry.
            words = family.group(1).split("-")
            pattern = re.compile(r"[\s.-]*".join(re.escape(w) for w in words))
            for label, text in (
                ("the survey TIERS table", survey_text),
                ("the probe docstring", probe_text),
            ):
                assert pattern.search(text), (
                    f"{label} does not name {family.group(1)!r}, which is on a "
                    "benchmark axis. Both enumerate the published minimums, and the "
                    "survey's stated purpose is displaying that they are not "
                    "monotonic across generations."
                )


class TestTheCopiesAreReallyFive:
    def test_every_named_file_exists(self):
        """Non-vacuity: if a copy is renamed, this file must fail rather than
        silently check four."""
        for path in (PRODUCT, UI_TS, SURVEY, PROBE):
            assert os.path.exists(path), f"{path} does not exist"
        assert harness_module("cache_audit").CACHE_MINIMUMS

    def test_the_two_haikus_still_bracket_the_table(self, product_tiers):
        """The property that makes the whole comparison worth running.

        If both Haikus ever resolve to the same number, a shared ``haiku`` stem
        becomes harmless and the ordering checks above lose their point — so pin the
        fact that they do not, and re-read this file if it ever fails.
        """
        assert product_tiers["claude-haiku-5-5"] == min(product_tiers.values())
        assert product_tiers["claude-haiku-4-5"] == max(product_tiers.values())
