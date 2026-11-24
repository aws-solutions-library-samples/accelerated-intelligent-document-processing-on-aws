# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The values a routing table reads from a document.

A routing rule's condition names an input from the catalogue in
``idp_common.config.routing_validation`` (``ROUTING_INPUTS``), sometimes with a
path. ``build_inputs`` computes the inputs the table's enabled rules use, and
only those, keyed by the text of the condition's column (``expression_for``),
which is what the decision table evaluates against.

Some inputs are on the document object. Others are in the section results and in
the policy rule validation summary in S3: each section result is read at most
once, in parallel, and the summary once. ``InputReader`` holds those reads, so
one reader serves the table's inputs, the placeholders of a reason and the list
of failed policy rules.

This module must stay importable with the extras the routing Lambda installs: it
must not import ``idp_common.assessment`` or ``idp_common.rule_validation``.
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, Field

from idp_common.config.routing_models import RoutingCondition, RoutingTable
from idp_common.config.routing_reason import cut_bytes
from idp_common.config.routing_validation import (
    class_names,
    routing_input,
    split_field_path,
)
from idp_common.field_paths import FieldPathError, extract_path
from idp_common.field_values import collect_missing_required_fields
from idp_common.section_exclusion import is_section_excluded

from .reasons import (
    count_confidence_reasons,
    failed_extraction_issue,
    route_missing_required_fields,
)
from .routing_dmn import expression_for

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_WORKERS",
    "FailedPolicyRule",
    "InputReader",
    "RoutingInputs",
    "SectionFacts",
    "build_inputs",
    "load_section_facts",
]

#: The most section results read at the same time.
MAX_WORKERS = 8

_VERDICT_FAIL = "fail"
_AGGREGATE_COUNT = "count"

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
#: A page reference that is a page number: digits only, and few enough of them
#: to fit the ``Int`` the API returns.
_PAGE_NUMBER_RE = re.compile(r"^[0-9]{1,9}$")


class RoutingInputs(BaseModel):
    """The input values of one evaluation, and what it took to get them."""

    #: The value of each input the table uses, by the text of its column.
    values: Dict[str, Any] = Field(default_factory=dict)
    #: How many objects were read from S3.
    reads: int = 0
    #: References that could not be resolved: a class the configuration does not
    #: have, a section result that could not be read, a summary that is not there.
    stale: List[str] = Field(default_factory=list)


@dataclass(frozen=True)
class FailedPolicyRule:
    """A policy rule the document failed, as the summary reports it."""

    #: ``<policy type>.<rule key>``.
    rule_id: str
    #: The policy the rule belongs to.
    policy: str
    #: The rule's key: the id without its policy type.
    name: str
    #: The explanation the validation stored, as one line of plain text.
    reason: str
    #: The pages the explanation cites.
    pages: tuple[int, ...]


@dataclass(frozen=True)
class SectionFacts:
    """What a section's stored result says."""

    #: A reviewer confirmed the section's values (``review.confirmed``).
    confirmed: bool = False
    inference_result: Any = None
    explainability_info: Any = None
    #: The result was read. False when the read failed.
    readable: bool = True


def _sections(document: Any) -> List[Any]:
    """The document's sections that are processed (not excluded)."""
    return [
        section
        for section in list(getattr(document, "sections", None) or [])
        if not is_section_excluded(section)
    ]


def _s3(s3: Any) -> Any:
    if s3 is not None:
        return s3
    from idp_common import s3 as s3_module

    return s3_module


def load_section_facts(
    document: Any, s3: Any = None, max_workers: int = MAX_WORKERS
) -> Dict[str, SectionFacts]:
    """The stored result of every processed section that has one, by section id.

    Each result is read once, with at most ``max_workers`` reads at a time. A
    section with no ``extraction_result_uri`` has no entry: it has no facts and
    is not confirmed. A result that cannot be read gives an entry with
    ``readable`` false, and the failure is logged.
    """
    targets = [
        (str(section.section_id), section.extraction_result_uri)
        for section in _sections(document)
        if getattr(section, "extraction_result_uri", None)
    ]
    if not targets:
        return {}
    reader = _s3(s3)

    def read(target: tuple) -> tuple:
        section_id, uri = target
        try:
            result = reader.get_json_content(uri)
        except Exception as error:  # noqa: BLE001 — reported as a stale reference
            logger.warning(
                "Could not read the result of section %s for routing: %s",
                section_id,
                error,
            )
            return section_id, SectionFacts(readable=False)
        if not isinstance(result, Mapping):
            return section_id, SectionFacts(readable=False)
        review = result.get("review")
        return section_id, SectionFacts(
            confirmed=isinstance(review, Mapping) and review.get("confirmed") is True,
            inference_result=result.get("inference_result"),
            explainability_info=result.get("explainability_info"),
        )

    workers = max(1, min(max_workers, len(targets)))
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="routing-inputs"
    ) as pool:
        return dict(pool.map(read, targets))


def _assessment_of(explainability_info: Any) -> Any:
    """The assessment a section result carries: a dict, or a list holding one."""
    if isinstance(explainability_info, list):
        first = explainability_info[0] if explainability_info else None
        return first if isinstance(first, dict) else None
    return explainability_info if isinstance(explainability_info, dict) else None


def _class_schema(config: Any, class_name: Optional[str]) -> Any:
    if not class_name or config is None:
        return None
    from idp_common.config.schema_utils import find_class_schema

    return find_class_schema(class_name, getattr(config, "classes", None))


def _policy_summary_uri(document: Any) -> Optional[str]:
    """The summary JSON beside the report ``rule_validation_result`` points at."""
    result = getattr(document, "rule_validation_result", None)
    uri = getattr(result, "output_uri", None)
    if not uri:
        return None
    return uri[: -len(".md")] + ".json" if uri.endswith(".md") else uri


def _policy_groups(summary: Any) -> List[tuple[Any, List[Mapping[str, Any]]]]:
    """The rule entries of a summary, by policy."""
    details = summary.get("rule_details") if isinstance(summary, Mapping) else None
    if not isinstance(details, Mapping):
        return []
    groups = []
    for name, policy in details.items():
        entries = policy.get("rules") if isinstance(policy, Mapping) else None
        groups.append((name, [e for e in entries or [] if isinstance(e, Mapping)]))
    return groups


def _policy_rules(summary: Any, policy_type: str = "") -> List[Mapping[str, Any]]:
    """The rule entries of a summary, of every policy or of one."""
    return [
        entry
        for name, entries in _policy_groups(summary)
        if not policy_type or name == policy_type
        for entry in entries
    ]


def _one_line(text: Any) -> str:
    """``text`` as one line: runs of whitespace are one space, and control
    characters are removed. Anything that is not text reads as empty."""
    if not isinstance(text, str):
        return ""
    return " ".join(_CONTROL_RE.sub("", " ".join(text.split())).split())


def _page_numbers(pages: Any, limit: int) -> tuple[int, ...]:
    """The page references that are page numbers, the first ``limit`` of them."""
    if not isinstance(pages, (list, tuple)):
        return ()
    numbers: List[int] = []
    for page in pages:
        if len(numbers) >= limit:
            break
        text = "" if isinstance(page, bool) else str(page).strip()
        if _PAGE_NUMBER_RE.match(text):
            numbers.append(int(text))
    return tuple(numbers)


class InputReader:
    """One evaluation's reads, made when the first input needs them.

    Each section result is read at most once and the policy rule validation
    summary at most once, whatever asks: the table's inputs (``build_inputs``),
    a placeholder of a reason (``value``) or the failed policy rules
    (``failed_policy_rules``).
    """

    def __init__(
        self,
        document: Any,
        config: Any,
        mode: str,
        s3: Any = None,
        max_workers: int = MAX_WORKERS,
    ):
        self.document = document
        self.config = config
        self.mode = mode
        self._s3 = s3
        self._max_workers = max_workers
        self.sections = _sections(document)
        self.reads = 0
        self.stale: List[str] = []
        self._facts: Optional[Dict[str, SectionFacts]] = None
        self._summary: Any = None
        self._summary_read = False

    def _note_stale(self, reference: str) -> None:
        if reference not in self.stale:
            self.stale.append(reference)

    # ----- the two kinds of read ----------------------------------------------

    def facts(self) -> Dict[str, SectionFacts]:
        if self._facts is None:
            self._facts = load_section_facts(self.document, self._s3, self._max_workers)
            self.reads += len(self._facts)
            for section_id, facts in self._facts.items():
                if not facts.readable:
                    self._note_stale(f"section:{section_id}")
        return self._facts

    def summary(self) -> Any:
        """The policy rule validation summary, or ``None`` when there is none."""
        if not self._summary_read:
            self._summary_read = True
            uri = _policy_summary_uri(self.document)
            if uri:
                self.reads += 1
                try:
                    self._summary = _s3(self._s3).get_json_content(uri)
                except Exception as error:  # noqa: BLE001 — a stale reference
                    logger.warning(
                        "Could not read the policy rule validation summary for "
                        "routing: %s",
                        error,
                    )
        return self._summary

    # ----- what the sections say ----------------------------------------------

    def _confirmed(self) -> List[str]:
        facts = self.facts()
        return [sid for sid, section in facts.items() if section.confirmed]

    def _confidence_alert_count(self) -> int:
        # Excluded sections are not processed, so they count nothing either.
        not_counted = set(self._confirmed()) | {
            str(section.section_id)
            for section in list(getattr(self.document, "sections", None) or [])
            if is_section_excluded(section)
        }
        return count_confidence_reasons(
            self.document, self.config, mode=self.mode, confirmed_sections=not_counted
        )

    def _extraction_failed_count(self) -> int:
        """The processed sections a reviewer has not confirmed whose extraction
        failed without failing the document (#2038): they have no fields and no
        confidence scores, so nothing else counts them."""
        confirmed = set(self._confirmed())
        return sum(
            1
            for section in self.sections
            if str(section.section_id) not in confirmed
            and failed_extraction_issue(section) is not None
        )

    def _missing_required_field_count(self) -> int:
        facts = self.facts()
        total = 0
        for section in self.sections:
            section_facts = facts.get(str(section.section_id))
            if section_facts is None or section_facts.confirmed:
                continue
            schema = _class_schema(
                self.config, getattr(section, "classification", None)
            )
            total += len(
                collect_missing_required_fields(
                    section_facts.inference_result,
                    schema,
                    _assessment_of(section_facts.explainability_info),
                )
            )
        return total

    def _reviewed(self) -> bool:
        if not self.sections:
            return False
        facts = self.facts()
        return all(
            (section_facts := facts.get(str(section.section_id))) is not None
            and section_facts.confirmed
            for section in self.sections
        )

    def _field_values(self, path: str) -> List[Any]:
        class_name, field_path = split_field_path(path)
        if class_name and _class_schema(self.config, class_name) is None:
            # The class is not in the configuration: the rule points at nothing.
            self._note_stale(f"class:{class_name}")
            return []
        wanted = class_name.lower()
        facts = self.facts()
        values = []
        for section in self.sections:
            section_class = str(getattr(section, "classification", "") or "")
            if wanted and section_class.lower() != wanted:
                continue
            section_facts = facts.get(str(section.section_id))
            if section_facts is None:
                continue
            try:
                value = extract_path(section_facts.inference_result, field_path)
            except FieldPathError:
                # Refused when the rules were saved; a path that still does not
                # parse reads nothing.
                self._note_stale(f"field:{path}")
                return []
            if value is not None:
                values.append(value)
        return values

    # ----- what the policy rule validation summary says -------------------------

    def _policy_rule_verdict(self, rule_id: str) -> Optional[str]:
        summary = self.summary()
        if summary is None:
            self._note_stale(f"policy_rule:{rule_id}")
            return None
        for rule in _policy_rules(summary):
            if rule.get("rule_id") == rule_id:
                verdict = rule.get("verdict")
                return verdict if isinstance(verdict, str) else None
        return None

    def _failed_policy_rule_count(self, policy_type: str) -> int:
        summary = self.summary()
        if summary is None:
            return 0
        return sum(
            1
            for rule in _policy_rules(summary, policy_type)
            if rule.get("verdict") == _VERDICT_FAIL
        )

    def failed_policy_rules(
        self, *, limit: int, reason_bytes: int, max_pages: int
    ) -> tuple[List[FailedPolicyRule], int]:
        """The policy rules the document failed, and how many there are.

        The first ``limit`` of them in the summary's order, each with its
        explanation as one line of at most ``reason_bytes`` bytes and its first
        ``max_pages`` page numbers. The count is of every failed rule, also
        those past the limit. With no summary there is none.

        Reads the summary at most once for the whole evaluation, and never
        raises: an entry that cannot be read is counted and left out.
        """
        kept: List[FailedPolicyRule] = []
        count = 0
        try:
            groups = _policy_groups(self.summary())
        except Exception:  # noqa: BLE001 — a summary that is not the expected shape
            logger.warning("event=routing_policy_failures_unreadable", exc_info=True)
            return [], 0
        for policy, entries in groups:
            for entry in entries:
                if entry.get("verdict") != _VERDICT_FAIL:
                    continue
                count += 1
                if len(kept) >= limit:
                    continue
                try:
                    rule_id = entry.get("rule_id")
                    rule_id = rule_id if isinstance(rule_id, str) else ""
                    kept.append(
                        FailedPolicyRule(
                            rule_id=rule_id,
                            policy=str(policy),
                            name=rule_id.partition(".")[2] or rule_id,
                            reason=cut_bytes(
                                _one_line(entry.get("reasoning")), reason_bytes
                            ),
                            pages=_page_numbers(
                                entry.get("supporting_pages"), max_pages
                            ),
                        )
                    )
                except Exception:  # noqa: BLE001 — one entry; the others stand
                    logger.warning(
                        "event=routing_policy_failure_unreadable", exc_info=True
                    )
        return kept, count

    # ----- which classes the document has -------------------------------------------

    def _class_present(self, path: str) -> Optional[bool]:
        wanted = path.strip().lower()
        known = {
            name.lower() for name in class_names(getattr(self.config, "classes", None))
        }
        if wanted not in known:
            # The class is not in the configuration: the rule points at nothing.
            self._note_stale(f"class:{path}")
            return None
        return any(
            str(getattr(section, "classification", None) or "").lower() == wanted
            for section in self.sections
        )

    # ----- one condition ---------------------------------------------------------

    def value(self, condition: RoutingCondition) -> Any:
        name = condition.input
        if name == "document_class":
            first = self.sections[0] if self.sections else None
            return getattr(first, "classification", None) or None
        if name == "section_classes":
            return self._aggregated(
                [
                    section.classification
                    for section in self.sections
                    if getattr(section, "classification", None)
                ],
                condition,
            )
        if name == "class_present":
            return self._class_present(condition.path)
        if name == "page_count":
            return int(getattr(self.document, "num_pages", 0) or 0)
        if name == "section_count":
            return len(self.sections)
        if name == "confidence_alert_count":
            return self._confidence_alert_count()
        if name == "missing_required_field_count":
            return self._missing_required_field_count()
        if name == "extraction_failed_count":
            return self._extraction_failed_count()
        if name == "route_missing_required_fields":
            return route_missing_required_fields(self.config)
        if name == "field_value":
            return self._aggregated(self._field_values(condition.path), condition)
        if name == "policy_rule_verdict":
            return self._policy_rule_verdict(condition.path)
        if name == "failed_policy_rule_count":
            return self._failed_policy_rule_count(condition.path)
        if name == "reviewed":
            return self._reviewed()
        if name == "processing_mode":
            return self.mode
        # Refused when the rules were saved. An input this code does not know
        # reads as nothing, so a test on it does not hold.
        self._note_stale(f"input:{name}")
        return None

    @staticmethod
    def _aggregated(values: Sequence[Any], condition: RoutingCondition) -> Any:
        """A list input as its column reads it: the list, or how many it holds."""
        entry = routing_input(condition.input)
        if (
            entry is not None
            and entry.is_list
            and condition.aggregate == _AGGREGATE_COUNT
        ):
            return len(values)
        return list(values)


def build_inputs(
    document: Any,
    config: Any,
    table: RoutingTable,
    *,
    mode: Literal["pipeline", "bda"],
    s3: Any = None,
    max_workers: int = MAX_WORKERS,
    reader: Optional[InputReader] = None,
) -> RoutingInputs:
    """The inputs ``table``'s enabled rules read from ``document``.

    ``values`` is keyed by the text of each condition's column, so it is the
    context ``idp_common.decision_tables.evaluate`` takes for
    ``routing_dmn.to_decision_table(table)``.

    With ``reader``, the values are read through it and ``mode``, ``s3`` and
    ``max_workers`` are the reader's own. What it read stays with it, so a
    reason's placeholders and the failed policy rules read nothing twice.

    | Input | Value |
    |---|---|
    | ``document_class`` | The class of the first processed section, else ``None`` |
    | ``section_classes`` | The classes of the processed sections |
    | ``class_present`` | A processed section has the named class, compared without regard to case; false when no section has a class; ``None`` for a class the configuration does not have |
    | ``page_count`` | ``document.num_pages`` |
    | ``section_count`` | How many sections are processed |
    | ``confidence_alert_count`` | The confidence reasons of the sections a reviewer has not confirmed (``count_confidence_reasons``) |
    | ``missing_required_field_count`` | The required fields those sections' results leave absent |
    | ``extraction_failed_count`` | Those sections whose extraction failed without failing the document: an error-severity ``extraction_failed`` issue (``reasons.failed_extraction_issue``) |
    | ``route_missing_required_fields`` | ``hitl.route_missing_required_fields``; true when unset |
    | ``field_value`` | The value at the path in every processed section's result (of the named class, when one is named); absent values are left out |
    | ``policy_rule_verdict`` | The rule's verdict in the summary; ``None`` when the rule or the summary is not there |
    | ``failed_policy_rule_count`` | The rules whose verdict is ``fail``, of every policy or of the named one; 0 with no summary |
    | ``reviewed`` | Every processed section's result is confirmed by a reviewer; false with no such section |
    | ``processing_mode`` | ``pipeline`` or ``bda`` |

    A processed section is one that is not excluded. A list input with the
    ``count`` aggregate is the number of values.

    A reference that cannot be resolved reads as ``None``, an empty list or 0,
    and is named in ``stale``. Nothing here raises for a document or a
    configuration it cannot read.
    """
    if reader is None:
        reader = InputReader(document, config, mode, s3, max_workers)
    values: Dict[str, Any] = {}
    for rule in table.rules:
        if not rule.enabled:
            continue
        for condition in rule.conditions:
            expression = expression_for(condition)
            if expression not in values:
                values[expression] = reader.value(condition)
    if reader.stale:
        logger.warning(
            "event=routing_stale_reference table=%s references=%s",
            table.id,
            ",".join(reader.stale),
        )
        from .service import _put_metric

        _put_metric("RoutingStaleReference", len(reader.stale))
    return RoutingInputs(values=values, reads=reader.reads, stale=list(reader.stale))
