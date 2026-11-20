# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Limitation detectors: what a run observed, mapped to solution-profile refs.

Every detector reads one recorded document (see ``collect.py`` for its shape) and
the processing profile's settings, and returns ``Finding`` objects. A finding always
carries the warning text saying what was seen; it carries a ref only when the
observation is the condition the limitation describes. A detector never infers a
fact it did not observe: no language is named, no page cap is assumed, and a
missing input (a BDA-mode document has no OCR markdown) produces no finding.

Pre-upload refusals are made in ``inputs.py``, which records the ref with the file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from idp_sdk._core.fit_check.refs import (
    LANGUAGE_SCRIPT,
    PACKET_LONG_CONTEXT,
    SIMPLE_PAGE_LIMIT,
    SIMPLE_ROW_LIMIT,
)
from idp_sdk._core.fit_check.scripts import (
    count_scripts,
    merge_counts,
    observed_non_latin,
)

#: The largest list the solution profile records as extracted completely in simple
#: mode with the shipped default model (limitation simple-extraction-row-limit: every
#: run complete at 400 rows; 800 not). A table larger than this was not measured as
#: safe. The figure depends on the model, which the warning says.
SIMPLE_MODE_MEASURED_COMPLETE_ROWS = 400

#: The issue code extraction records when a list returns fewer rows than the
#: schema's ``minItems`` (idp_common/extraction/service.py).
LIST_TRUNCATED_CODE = "extraction_list_truncated"

#: Text in an error that Bedrock or the extraction step writes when a request was
#: refused for size. Matched literally, case-insensitively.
_INPUT_TOO_LARGE_MARKERS = ("extractioninputtoolarge", "input is too long")

#: Textract features whose languages are all written in the Latin script
#: (docs/languages.md): no features means DetectDocumentText, English only;
#: QUERIES is English only; FORMS covers six Western European languages.
_LATIN_ONLY_TEXTRACT_FEATURES = frozenset({"QUERIES", "FORMS"})


@dataclass(frozen=True)
class Finding:
    warning: str
    ref: Optional[str] = None


def _error_text(doc: Dict[str, Any]) -> str:
    parts = [doc.get("error") or ""] + list(doc.get("errors") or [])
    return "\n".join(str(p) for p in parts if p)


def _section_text(doc: Dict[str, Any], section: Dict[str, Any]) -> str:
    texts = doc.get("page_text") or {}
    return "\n\n".join(texts.get(str(pid), "") for pid in section.get("page_ids") or [])


def _largest_table_rows(text: str) -> Optional[int]:
    """Rows in the largest Markdown table in ``text`` (fragments split by a page
    break are merged by the parser); ``None`` when the parser is unavailable."""
    if not text.strip():
        return 0
    try:
        from idp_common.extraction.tools.table_parser import parse_markdown_tables
    except ImportError:
        return None
    parsed = parse_markdown_tables(text)
    tables = parsed.get("tables") or []
    counts = [int(t.get("row_count") or 0) for t in tables]
    if not counts and parsed.get("row_count"):
        counts = [int(parsed["row_count"])]
    return max(counts, default=0)


def _longest_list(inference_result: Any) -> int:
    if not isinstance(inference_result, dict):
        return 0
    return max(
        (len(v) for v in inference_result.values() if isinstance(v, list)), default=0
    )


def _issues(section: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    return section.get("issues") or []


def detect_table_rows(doc: Dict[str, Any], config: Dict[str, Any]) -> List[Finding]:
    """Large tables, and lists the extraction step reported as truncated."""
    if config.get("use_bda"):
        return []
    simple = (config.get("extraction_mode") or "simple") == "simple"
    findings: List[Finding] = []
    for section in doc.get("sections") or []:
        label = f"Section {section.get('section_id')} ({section.get('class')})"
        rows = _largest_table_rows(_section_text(doc, section))
        if rows is None:
            findings.append(
                Finding(f"{label}: table rows not counted (table parser unavailable)")
            )
        elif rows:
            extracted = _longest_list(section.get("inference_result"))
            findings.append(
                Finding(
                    f"{label}: the OCR text holds a table of {rows} rows; the longest "
                    f"list field extracted has {extracted} items"
                )
            )
            if simple and rows > SIMPLE_MODE_MEASURED_COMPLETE_ROWS:
                findings.append(
                    Finding(
                        f"{label}: {rows} table rows is above the "
                        f"{SIMPLE_MODE_MEASURED_COMPLETE_ROWS} rows the solution "
                        "profile records as extracted completely in simple mode with "
                        "the default model (the threshold depends on the model)",
                        SIMPLE_ROW_LIMIT,
                    )
                )
        if simple and any(
            i.get("code") == LIST_TRUNCATED_CODE for i in _issues(section)
        ):
            findings.append(
                Finding(
                    f"{label}: extraction reported a list shorter than its schema's "
                    "minItems in simple mode",
                    SIMPLE_ROW_LIMIT,
                )
            )
    return findings


def detect_refusal(doc: Dict[str, Any], config: Dict[str, Any]) -> List[Finding]:
    """A request refused for size, by the step that refused it."""
    text = _error_text(doc)
    lowered = text.lower()
    if not any(marker in lowered for marker in _INPUT_TOO_LARGE_MARKERS):
        return []
    pages = doc.get("pages")
    size = f" at {pages} pages" if pages else ""
    if "extractioninputtoolarge" in lowered or "extraction" in lowered:
        if config.get("use_bda") or (config.get("extraction_mode") or "simple") != (
            "simple"
        ):
            return [Finding(f"Extraction refused the request as too large{size}")]
        return [
            Finding(
                f"Simple extraction refused the section as too large for one "
                f"request{size}",
                SIMPLE_PAGE_LIMIT,
            )
        ]
    if "classif" in lowered:
        if config.get("classification_granularity") == "packet":
            return [
                Finding(
                    f"Packet-granularity classification refused the request as too "
                    f"long{size}",
                    PACKET_LONG_CONTEXT,
                )
            ]
        return [Finding(f"Classification refused the request as too long{size}")]
    return [Finding(f"A model request was refused as too long{size}")]


def _latin_only_ocr(config: Dict[str, Any]) -> Optional[str]:
    """A description of the OCR configuration when it reads Latin script only."""
    if config.get("use_bda"):
        return (
            "BDA mode, whose listed languages are all written in the Latin script "
            "and which does not read vertical text"
        )
    if (config.get("ocr_backend") or "textract") != "textract":
        return None
    features = [str(f).upper() for f in config.get("ocr_features") or []]
    if not features:
        return (
            "the Textract backend with no OCR features (DetectDocumentText, English "
            "only)"
        )
    narrow = sorted(set(features) & _LATIN_ONLY_TEXTRACT_FEATURES)
    if narrow:
        return (
            f"the Textract backend with features {features}, of which {narrow} "
            "cover Latin-script languages only"
        )
    return None


def detect_script(doc: Dict[str, Any], config: Dict[str, Any]) -> List[Finding]:
    """Non-Latin script observed in a document whose OCR reads Latin script only."""
    coverage = _latin_only_ocr(config)
    if coverage is None:
        return []
    layer = observed_non_latin(doc.get("text_layer_scripts") or {})
    ocr = observed_non_latin(
        merge_counts(*(count_scripts(t) for t in (doc.get("page_text") or {}).values()))
    )
    findings = []
    for source, scripts in (("the PDF text layer", layer), ("the OCR text", ocr)):
        if not scripts:
            continue
        seen = ", ".join(f"{s} ({n} letters)" for s, n in scripts.items())
        findings.append(
            Finding(
                f"{seen} observed in {source}; the profile processed it with "
                f"{coverage}",
                LANGUAGE_SCRIPT,
            )
        )
    return findings


DETECTORS = (detect_table_rows, detect_refusal, detect_script)


def run_detectors(doc: Dict[str, Any], config: Dict[str, Any]) -> List[Finding]:
    findings: List[Finding] = []
    for detector in DETECTORS:
        findings.extend(detector(doc, config))
    return findings
