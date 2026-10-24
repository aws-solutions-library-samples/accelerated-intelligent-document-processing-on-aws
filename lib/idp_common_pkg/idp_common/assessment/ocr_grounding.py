# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Ground assessment ``explainability_info`` geometry in real OCR data.

After the assessment LLM produces per-field bounding boxes (LLM-estimated, on a
0-1000 scale that the service rescales to 0-1), this module matches each extracted
attribute *value* against the real OCR lines in the consolidated ``pageData.json``
artifact (LINE-primary, geometry already normalized 0-1) and, on a confident match,
replaces the LLM-estimated box with the real OCR box and tags its provenance.

Design notes (see scratch/assessment-ocr-grounding-plan.md):

- **Coordinate space:** ``pageData`` geometry is already 0-1, so grounded boxes skip
  the /1000 rescale that LLM boxes go through. This module never rescales; it only
  ever produces 0-1 boxes, matching what the UI bounding-box renderer consumes.
- **Backward compatible / safe fallback:** when ``pageData.json`` is absent (older
  documents, missing URI), a page has ``geometryAvailable: false`` (plain LLM OCR,
  Chandra, ``none`` backend), or no OCR line matches the extracted value, the
  field's existing LLM-estimated box is left untouched. Worst case == today.
- **explainability_info contract:** the grounded box *replaces* the contents of the
  existing ``geometry`` array (same shape, ``[{boundingBox:{left,top,width,height},
  page}]``, 0-1, 1-indexed page). Two additive keys are written on the field
  assessment: ``geometry_source`` ("ocr" | "ocr-paragraph" | "ocr-normalized" |
  "ocr-fuzzy" | "ocr-weak" | "llm") and, when a
  matched line carries OCR confidence, ``ocr_confidence`` (0-1). The LLM
  ``confidence`` / ``confidence_reason`` are never overwritten, so HITL thresholds
  and ``confidence_threshold_alerts`` are unaffected.
"""

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from idp_common import s3

logger = logging.getLogger(__name__)

# Minimum Jaccard token-overlap for a fuzzy single-line match.
_FUZZY_MATCH_THRESHOLD = 0.6

# Minimum character-level Levenshtein similarity for the last-resort fuzzy tier
# (matches the evaluation module's default FUZZY threshold). Catches OCR noise /
# near-misses (e.g. "Acme Corp" vs "Acrne Corp"), NOT reformatting — that is
# handled by format variants + type-aware equality below.
_LEVENSHTEIN_MATCH_THRESHOLD = 0.8

# Cap on how many adjacent lines a single value may span (multi-line union).
# Keeps unions tight; multi-line addresses/cells are typically 2-4 lines.
_MAX_SPAN_LINES = 6

#: Separator punctuation collapsed to a space *inside* a string before matching.
#:
#: The defect this closes is in the SPAN tier rather than in anything about lists
#: (#1520). A value written across two OCR lines carries a separator **at the line
#: break that the page does not have**: the document reads ``"510 W. 24th Street,
#: #2"`` then ``"Sioux Falls, SD 57105"``, and extraction returns the two joined by
#: ``", "``. :func:`_collect_span_candidates` joins consecutive lines with a single
#: space and requires exact equality, so that one extra comma defeats the span and
#: the field falls back to whatever fragment matched — measured cases include a box
#: on the OCR line ``"1"`` for a full street address and on ``"A."`` for a thesis
#: title. Collapsing the separators on both sides makes the two agree.
#:
#: ⚠️ **Only ``,`` and ``;``, and the exclusions are measured rather than cautious.**
#: Adding ``/`` and ``|`` was tried over the same corpus and is worse on both counts:
#: it loses a box, and it moves five date boxes to a *different* rendering of the
#: same date (two of them onto another page), because ``/`` is the separator
#: :func:`_value_variants` builds its ``%m/%d/%Y`` forms from — so stripping it lets
#: a reformatted variant outrank the line carrying the value verbatim. ``/`` is also
#: how a model composes a label that is nowhere on the page (``"Request/Approval"``,
#: ``"Yes/No"``), and giving those a box is the #1519 harm rather than a fix.
#:
#: ⚠️ **The trailing ``\s`` is the load-bearing part, not a tidiness detail.** A
#: separator a model inserted at a join is followed by whitespace (``", "``); one
#: *inside* a token is digit grouping or OCR noise and must be left alone. Collapsing
#: those too costs 60 boxes on a deliberately-garbled OCR corpus: the line
#: ``"2,6,6914"`` is a mangled ``"2,669.14"`` that the Levenshtein tier matches at
#: 0.875, and substituting a space **adds a character that was never there**, pushing
#: the character distance past the threshold. Those boxes were correct — they sat on
#: the amount — so the narrower rule keeps a tier this one has no business touching.
#:
#: This runs on **both** an extracted value and an OCR line, since one function
#: normalizes both. It can therefore only make the two *agree* about punctuation
#: they already differ on; it never adds text to either side.
_INTERNAL_SEPARATORS = re.compile(r"[,;]+\s")


def _normalize(text: Any) -> str:
    """Casefold, collapse separators and whitespace, strip surrounding punctuation."""
    if text is None:
        return ""
    s = str(text).casefold()
    # Internal separator punctuation -> space, so a value and the OCR line it came
    # from compare equal when only their separators differ (see the constant).
    s = _INTERNAL_SEPARATORS.sub(" ", s)
    # Collapse all whitespace runs to single spaces.
    s = re.sub(r"\s+", " ", s).strip()
    # Strip leading/trailing punctuation that commonly differs between an
    # extracted value and the OCR line it came from (e.g. "$1,234.00" vs "1,234.00").
    s = s.strip(" \t\n\r.,;:!?\"'()[]{}<>|$%*#-_=")
    return s


def _tokens(text: str) -> set:
    """Word-token set of an already-normalized string."""
    return set(re.findall(r"[^\W_]+", text)) if text else set()


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if inter == 0:
        return 0.0
    return inter / len(a | b)


# --------------------------------------------------------------------------- #
# Format-aware matching helpers
#
# Extraction often canonicalizes a value to a schema-prescribed format that does
# NOT match how it is rendered in the document (and thus in OCR text): a date
# extracted as "2022-04-04" appears as "04/04/2022"; an amount "1234.00" appears
# as "$1,234.00"; a phone "+15551234567" appears as "(555) 123-4567". Plain text
# matching then fails and the field gets no geometry. These helpers bridge that
# gap two ways: (1) type-aware equality — parse both the value and an OCR line as
# the same logical type (date/number) and compare the parsed forms; (2) value
# variants — render the value in the common surface forms so the existing
# exact/substring/span matcher can hit. Both are precision-preserving: a parse
# must succeed on BOTH sides, so unrelated text never spuriously matches.
# --------------------------------------------------------------------------- #

# Hint values describing the logical type of a field, used to choose which
# format bridges to attempt. Resolved from the JSON-Schema (type/format) when
# available, else inferred from the value itself.
HINT_DATE = "date"
HINT_NUMBER = "number"
HINT_PHONE = "phone"
HINT_STRING = "string"


def _parse_date(text: Any):
    """Parse a string as a date, returning a ``date`` or None.

    Uses dateutil when available (handles most human formats). Conservative:
    requires the string to actually contain digits so plain words ("March")
    or arbitrary tokens don't parse into today's date.
    """
    if text is None:
        return None
    s = str(text).strip()
    if not s or not any(ch.isdigit() for ch in s):
        return None
    # A real date needs a separator or a month name — otherwise a bare number
    # like "1234.00" or "100" would parse as a date (dateutil is permissive).
    if not (re.search(r"[/\-.:]", s) or re.search(r"[A-Za-z]", s)):
        return None
    try:
        from dateutil import parser as _dateparser

        if len(re.sub(r"\D", "", s)) < 4:
            return None
        return _dateparser.parse(s, fuzzy=False).date()
    except Exception:  # noqa: BLE001 - any parse failure -> not a date
        return None


def _parse_number(text: Any) -> Optional[float]:
    """Parse a string as a number, stripping currency/commas/parens, or None."""
    if text is None:
        return None
    s = str(text).strip()
    if not s or not any(ch.isdigit() for ch in s):
        return None
    # Accounting negatives: (1,234.00) -> -1234.00
    neg = s.startswith("(") and s.endswith(")")
    cleaned = re.sub(r"[,$%\s()]", "", s)
    # Reject things that aren't basically a number (e.g. "12 Main St" -> "12MainSt").
    if not re.fullmatch(r"[-+]?\d*\.?\d+", cleaned):
        return None
    try:
        val = float(cleaned)
        return -val if neg else val
    except (ValueError, TypeError):
        return None


def _digits(text: Any) -> str:
    """All digits in a string (for phone-number comparison)."""
    return re.sub(r"\D", "", str(text)) if text is not None else ""


def _levenshtein_sim(a: str, b: str) -> float:
    """Character-level Levenshtein similarity (0-1), reusing the evaluation module.

    Returns 0.0 on import failure (the tier is then simply inert). Guarded so an
    empty/degenerate string never claims a spurious match.
    """
    if not a or not b:
        return 0.0
    try:
        from idp_common.evaluation.text_matching import fuzz_score

        return fuzz_score(a, b)
    except Exception:  # noqa: BLE001 - keep grounding resilient if eval is absent
        return 0.0


def _stripped_len(text: str) -> Optional[int]:
    """Length of ``text`` after the stripping :func:`fuzz_score` applies.

    Split out so a caller sweeping many lines against ONE value can compute the
    value's length once instead of per line — see the ``a_stripped_len`` argument
    of :func:`_levenshtein_can_reach`. None when the evaluation module is absent,
    which callers must read as "cannot bound" rather than as a length of zero.
    """
    try:
        from idp_common.evaluation.text_matching import strip_punctuation_space

        return len(strip_punctuation_space(text))
    except Exception:  # noqa: BLE001 - keep grounding resilient if eval is absent
        return None


def _levenshtein_can_reach(
    a: str, b: str, threshold: float, a_stripped_len: Optional[int] = None
) -> bool:
    """Could character similarity between ``a`` and ``b`` reach ``threshold``?

    An **exact** bound, not a heuristic, so skipping on a False answer cannot
    change any result. :func:`fuzz_score` is ``1 - distance / max(len)`` over
    punctuation-stripped strings, and Levenshtein distance is at least the
    difference in length, so::

        similarity <= 1 - |la - lb| / max(la, lb) == min(la, lb) / max(la, lb)

    When that ceiling is below the threshold the O(n·m) dynamic program cannot
    produce a match and is pure cost. It is computed over the SAME stripping
    ``fuzz_score`` applies, because stripping changes both lengths and a bound
    taken before it is not a bound after it.

    Why it is worth having: this sweep runs whenever no whole-string candidate
    exists, and #1519 made that far more common by stopping a ``_TIER_WEAK_PARTIAL``
    fragment from short-circuiting it. Reaching the verdict "nothing here resembles
    this value" then means a full DP against every line on the page, for every long
    value. Measured over 50 real sections, grounding went from 4.6s to 25.4s with
    98% of it inside ``fuzz_score`` at 165M inner comparisons; with this guard it is
    0.5s and the grounding results are identical. That cost lands in the Assessment
    Lambda, which has a wall-clock budget, so leaving it would trade wrong boxes for
    unscored rows.

    Returns True when the bound cannot be computed, so a missing evaluation
    module leaves behaviour unchanged rather than silently narrowing the tier.
    """
    if not a or not b:
        return False
    try:
        from idp_common.evaluation.text_matching import strip_punctuation_space

        la = _stripped_len(a) if a_stripped_len is None else a_stripped_len
        lb = len(strip_punctuation_space(b))
    except Exception:  # noqa: BLE001 - keep grounding resilient if eval is absent
        return True
    if la is None:
        return True
    if not la or not lb:
        return False
    return min(la, lb) / max(la, lb) >= threshold


def _infer_hint(value: Any) -> str:
    """Infer a field's logical type from the value itself (heuristic fallback).

    Order matters:
    - PHONE before NUMBER: a phone-shaped value ("+1 (555) 123-4567") would parse
      as a number and then miss digit-suffix matching against "(555) 123-4567".
      Requires 7-15 digits AND phone punctuation (+, (), -, space) and nothing
      else, so a bare integer falls through to NUMBER.
    - NUMBER before DATE: a genuine date ("04/04/2022") fails the strict numeric
      regex, while a bare number ("1234.00") must NOT be mistaken for a date
      (dateutil is permissive).
    """
    s = str(value).strip() if value is not None else ""
    digits = _digits(value)
    # PHONE: 7-15 digits with phone punctuation and nothing else, AND not a date
    # ("2022-04-04" is digits+dashes but is a date) and not a plain decimal.
    if (
        7 <= len(digits) <= 15
        and len(digits) == len(re.sub(r"[\s\-().+]", "", s))
        and re.search(r"[()+]|\d[\s\-]\d", s)
        and "." not in s
        and _parse_date(value) is None
    ):
        return HINT_PHONE
    if _parse_number(value) is not None:
        return HINT_NUMBER
    if _parse_date(value) is not None:
        return HINT_DATE
    return HINT_STRING


# JSON-Schema 'format' values that imply a date.
_SCHEMA_DATE_FORMATS = {"date", "date-time", "datetime"}
_SCHEMA_NUMBER_TYPES = {"number", "integer"}


def _resolve_hint(value: Any, schema_hint: Optional[Dict[str, Any]]) -> str:
    """Resolve a field's logical type: JSON-Schema hint first, else value-based.

    ``schema_hint`` is the field's JSON-Schema fragment (``{"type":..,"format":..}``).
    When it names a date/number type we trust it; otherwise (or absent) we infer
    from the value so any config benefits without declaring formats.
    """
    if isinstance(schema_hint, dict):
        fmt = str(schema_hint.get("format", "")).lower()
        typ = str(schema_hint.get("type", "")).lower()
        if fmt in _SCHEMA_DATE_FORMATS:
            return HINT_DATE
        if typ in _SCHEMA_NUMBER_TYPES:
            return HINT_NUMBER
        # A string field whose value still looks like a date/number/phone is
        # bridged via the value-based inference (the schema didn't forbid it).
    return _infer_hint(value)


def _value_variants(value: Any, hint: str) -> List[str]:
    """Surface-form renderings of ``value`` to try against OCR text.

    Always includes the value as-is. For dates/numbers, adds the common ways the
    same logical value is written in documents so the exact/substring matcher can
    hit despite reformatting. All variants are returned RAW (the caller normalizes).
    """
    variants: List[str] = [str(value)] if value is not None else []
    if hint == HINT_DATE:
        d = _parse_date(value)
        if d is not None:
            for fmt in (
                "%m/%d/%Y",
                "%-m/%-d/%Y",
                "%d/%m/%Y",
                "%m-%d-%Y",
                "%d-%m-%Y",
                "%Y/%m/%d",
                "%Y-%m-%d",
                "%B %d, %Y",
                "%b %d, %Y",
                "%d %B %Y",
                "%d %b %Y",
                "%m/%d/%y",
                "%-m/%-d/%y",
            ):
                try:
                    variants.append(d.strftime(fmt))
                except ValueError:
                    continue
    elif hint == HINT_NUMBER:
        n = _parse_number(value)
        if n is not None:
            # Integer-valued -> also a no-decimal form; always a thousands form.
            as_int = int(n) if n == int(n) else None
            cores = []
            if as_int is not None:
                cores += [f"{as_int}", f"{as_int:,}"]
            cores += [f"{n:.2f}", f"{abs(n):,.2f}", f"{n}"]
            for core in cores:
                variants.append(core)
                variants.append(f"${core}")
    return variants


def _type_equal(value: Any, line_text: Any, hint: str) -> bool:
    """True when ``value`` and an OCR line are the SAME logical value by type.

    This is the robust bridge for reformatting (e.g. 2022-04-04 == 04/04/2022),
    where character/­token similarity is low but the parsed values are identical.
    Requires a successful parse on BOTH sides, so it never matches unrelated text.
    """
    if hint == HINT_DATE:
        dv = _parse_date(value)
        return dv is not None and dv == _parse_date(line_text)
    if hint == HINT_NUMBER:
        nv = _parse_number(value)
        nl = _parse_number(line_text)
        return nv is not None and nl is not None and nv == nl
    if hint == HINT_PHONE:
        dv = _digits(value)
        dl = _digits(line_text)
        if len(dv) < 7 or len(dl) < 7:
            return False
        # Exact, or one is a suffix of the other (country-code prefix differs,
        # e.g. "+1 555 123 4567" vs "(555) 123-4567"). Bounded to phone lengths.
        return dv == dl or (
            len(dl) <= 15 and len(dv) <= 15 and (dv.endswith(dl) or dl.endswith(dv))
        )
    return False


def _is_digit(ch: str) -> bool:
    """An ASCII digit. ``str.isdigit`` also accepts superscripts such as ``"²"``."""
    return "0" <= ch <= "9"


#: A date written with slashes: two slashes (``12/17/2024``, ``2024/12/17``), or
#: one slash beside a four-digit year (``12/2024``). Matched against the whole
#: maximal run of digits and slashes around a slash.
_SLASH_DATE = re.compile(r"\d{1,4}/\d{1,2}/\d{1,4}|\d{1,2}/\d{4}|\d{4}/\d{1,2}")
#: The longest such run (``2024/12/17``); a longer run is not a date.
_SLASH_DATE_MAX_LEN = 10


def _slash_in_date(text: str, i: int) -> bool:
    """Is the slash at ``text[i]`` part of a date, rather than a ratio or fraction?

    Only a date's slash joins digits into one run. ``120/80`` (blood pressure),
    ``75/100`` (a score), ``10/325`` (a dose) and ``3/12`` (page 3 of 12) print
    numbers that are values in their own right. The scan is bounded by the
    longest date, so it costs the same however long the line is.
    """
    lo = i
    while lo > 0 and i - lo <= _SLASH_DATE_MAX_LEN and text[lo - 1] in "0123456789/":
        lo -= 1
    hi = i + 1
    while (
        hi < len(text) and hi - i <= _SLASH_DATE_MAX_LEN and text[hi] in "0123456789/"
    ):
        hi += 1
    run = text[lo:hi]
    return len(run) <= _SLASH_DATE_MAX_LEN and _SLASH_DATE.fullmatch(run) is not None


def _joins_digit(text: str, i: int, step: int) -> bool:
    """Does the character at ``text[i]`` continue a digit run, reading by ``step``?

    True for a digit, and for a decimal point, thousands comma or date slash whose
    next character in the same direction is a digit (``"75"`` in ``"75.34"``,
    ``"1,275"`` or the date ``"12/17/2024"``). Anything else — space, ``$``,
    ``%``, a unit, a sign, a hyphen, a colon, a ratio's slash, closing punctuation,
    the end of the line — is a token boundary.

    A date's slash is there because a date component is part of a date, not a
    number printed on its own: without it a value with its decimals dropped (``17``
    for ``$17.49``) found a "whole" ``17`` inside ``12/17/2024``, which then
    outranked the line that really holds the amount. Only a date's slash joins —
    see :func:`_slash_in_date`.

    One exception, reading rightwards: a comma followed by one or two digits and
    then a non-digit is a **decimal comma** (``"1250,00"``), since a thousands group
    is always three digits. The integer part ends there, so it is a boundary. No
    surface form of a number renders a decimal comma, so for a European amount the
    integer part is the only form that can match at all, and demoting it would
    hand the box to any other line printing that integer.
    """
    if not 0 <= i < len(text):
        return False
    ch = text[i]
    if _is_digit(ch):
        return True
    j = i + step
    if not (ch in ".,/" and 0 <= j < len(text) and _is_digit(text[j])):
        return False
    if ch == "/":
        return _slash_in_date(text, i)
    if ch == "," and step > 0:
        run = 0
        while j + run < len(text) and _is_digit(text[j + run]):
            run += 1
        return run not in (1, 2)
    return True


def _containing_number(text: str, start: int, end: int) -> Tuple[int, int]:
    """Bounds of the run of digits joined by ``.`` or ``,`` holding ``[start:end]``."""
    while start > 0 and (
        _is_digit(text[start - 1])
        or (text[start - 1] in ".," and start >= 2 and _is_digit(text[start - 2]))
    ):
        start -= 1
    while end < len(text) and (
        _is_digit(text[end])
        or (text[end] in ".," and end + 1 < len(text) and _is_digit(text[end + 1]))
    ):
        end += 1
    return start, end


def _only_inside_longer_number(
    norm_variant: str, norm_text: str, value_number: Optional[float] = None
) -> bool:
    """True when every occurrence of a numeric variant in a line is digit-embedded.

    The question is whether the line prints the number as a whole token, or only
    contains its digits inside a longer number. An occurrence counts as whole when
    neither neighbour continues a digit run (see :func:`_joins_digit`); one whole
    occurrence anywhere in the line is enough. A side is only checked where the
    variant itself ends in a digit, so a sign or symbol that is part of the variant
    is never mistaken for a neighbour. Callers apply this to NUMBER values only.

    An occurrence also counts as whole when the longer number around it is the
    value itself written with more zeros (``"075.00"``, ``"75.000"``, or an item
    number ``"0075"`` for 75), compared via ``value_number`` (unsigned, as the
    unsigned variant already matches a value printed without its sign). Only a
    number joined by ``.`` or ``,`` is read this way, so a date component still
    counts as embedded.
    """
    if not norm_variant:
        return False
    check_left = _is_digit(norm_variant[0])
    check_right = _is_digit(norm_variant[-1])
    # The skip below relies on the longer number around an occurrence holding only
    # digits, "." and ",", which holds only when the variant itself does ("10 10"
    # can recur just past the number around its first occurrence).
    can_skip = all(_is_digit(c) or c in ".," for c in norm_variant)
    start = norm_text.find(norm_variant)
    if start == -1:
        return False
    while start != -1:
        end = start + len(norm_variant)
        left_joined = check_left and _joins_digit(norm_text, start - 1, -1)
        right_joined = check_right and _joins_digit(norm_text, end, 1)
        if not left_joined and not right_joined:
            return False
        resume = start + 1
        if value_number is not None:
            lo, hi = _containing_number(norm_text, start, end)
            if hi - lo > len(norm_variant) and _parse_number(norm_text[lo:hi]) == abs(
                value_number
            ):
                return False
            # Every later occurrence inside the same longer number is joined on
            # its left and has the same number around it, so it cannot decide
            # anything; skipping it keeps a long digit run linear, not quadratic.
            if can_skip:
                resume = max(resume, hi)
        start = norm_text.find(norm_variant, resume)
    return True


def load_page_ocr_data(
    pages: Dict[str, Any], page_ids: List[str], page_offset: int = 0
) -> Dict[int, Dict[str, Any]]:
    """
    Load ``pageData.json`` for each page in ``page_ids`` keyed by its **1-based
    SECTION-RELATIVE page number** (NOT the document-absolute page id).

    The returned key becomes ``geometry.page`` on every grounded field, and the UI
    navigates with ``sectionPageIds[geometry.page - 1]`` — i.e. it expects page 1 to
    be the FIRST page of the section. Keying by ``int(page_id)`` (document-absolute)
    made a section that starts at doc page 2 emit ``page: 2`` for its first page, so
    the viewer jumped to the wrong page (off by the section's start offset). Keying
    by position within the section fixes this for every grounding path (ocr_only,
    llm_grounded, standalone Assessment step, and the sharded agentic path).

    ``page_ids`` MUST be in section reading order (callers pass ``sorted_page_ids``).
    ``page_offset`` shifts the section-relative numbering for callers that pass only
    a *slice* of the section's pages — the sharded path passes
    ``sorted_page_ids[page_start:page_end]`` with ``page_offset=page_start`` so a
    shard's pages still number relative to the WHOLE section (e.g. a shard over
    section pages 6-10 yields keys 6-10, not 1-5).

    Pages without an ``ocr_page_data_uri`` (older documents) or whose artifact cannot
    be read are simply omitted from the result, so callers degrade gracefully — the
    section-relative numbering of the surviving pages is unaffected (it is derived
    from the page's position in ``page_ids``, not from how many loaded).

    Args:
        pages: Document.pages mapping (page_id -> Page).
        page_ids: Page IDs belonging to the section (or shard), in reading order.
        page_offset: 0-based offset of ``page_ids[0]`` within the full section.

    Returns:
        Mapping of ``section-relative 1-based page number`` -> parsed pageData dict,
        only for pages that have usable data.
    """
    result: Dict[int, Dict[str, Any]] = {}
    for local_idx, page_id in enumerate(page_ids):
        section_page = page_offset + local_idx + 1  # 1-based, section-relative
        page = pages.get(page_id)
        if page is None:
            continue
        uri = getattr(page, "ocr_page_data_uri", None)
        if not uri:
            continue
        try:
            page_data = s3.get_json_content(uri)
        except Exception as e:
            logger.warning(
                f"Could not read pageData.json for page {page_id} ({uri}): {e}"
            )
            continue
        if isinstance(page_data, dict):
            result[section_page] = page_data
    return result


def _union_box(boxes: List[Dict[str, float]]) -> Dict[str, float]:
    """Union a list of 0-1 ``{left,top,width,height}`` boxes into a bounding box."""
    lefts = [b["left"] for b in boxes]
    tops = [b["top"] for b in boxes]
    rights = [b["left"] + b["width"] for b in boxes]
    bottoms = [b["top"] + b["height"] for b in boxes]
    left = min(lefts)
    top = min(tops)
    return {
        "left": left,
        "top": top,
        "width": max(rights) - left,
        "height": max(bottoms) - top,
    }


def _line_box(line: Dict[str, Any]) -> Optional[Dict[str, float]]:
    """Return a line's 0-1 boundingBox dict, or None if it has no geometry."""
    geom = line.get("geometry")
    if not isinstance(geom, dict):
        return None
    bb = geom.get("boundingBox")
    if not isinstance(bb, dict):
        return None
    # Defensive: require the four expected keys.
    if not all(k in bb for k in ("left", "top", "width", "height")):
        return None
    return {
        "left": bb["left"],
        "top": bb["top"],
        "width": bb["width"],
        "height": bb["height"],
    }


# Match precision tiers (lower = more precise). The caller keeps only the best tier
# that matched, then disambiguates ties spatially. Ordering rationale:
#   EXACT     - line text equals the value
#   SUBSTRING - value contained in a single line (whole value covered)
#   SUBSTRING_EMBEDDED - a NUMBER contained in a single line only inside a longer
#               digit run ("75" in "08337510"); see _TIER_SUBSTRING_EMBEDDED
#   SPAN      - value reconstructed by joining consecutive lines (whole value covered)
#   PARTIAL   - a single line is a fragment of the value (partial coverage)
#   FUZZY     - token-overlap only
# Full-coverage matches (SUBSTRING/SPAN) rank above PARTIAL fragments so a multi-line
# value grounds to the unioned span rather than to one of its individual fragments.
_TIER_EXACT = 1
_TIER_SUBSTRING = 2
#: A NUMBER found in a line only as part of a longer digit run — ``"75"`` inside
#: the invoice id ``"08337510"``, ``"6.0"`` inside ``"126.00 total"`` (#1565).
#:
#: Number values are searched in several surface forms (``75.0`` is also tried as
#: ``"75"`` and ``"75.00"``), and a short form is contained by coincidence in any
#: longer number. Ranked at ``_TIER_SUBSTRING``, such a hit would tie with the line
#: that genuinely prints the value, and which box won would be decided by reading
#: order or by proximity to the LLM's estimate rather than by evidence.
#:
#: **Demotion by exactly one place, not rejection.** A digit run containing the
#: value is not always a coincidence: a model that drops the cents (``272243`` for
#: ``272243.34``) or misreads one digit of a long number still points at the right
#: line, and that is the case where a box matters most — the value is wrong and the
#: box is how a reviewer finds out. Rejecting the hit would remove those boxes.
#: Ranking it immediately below ``_TIER_SUBSTRING`` changes exactly one thing: a
#: line printing the number as a whole token now beats one where it is embedded.
#: Every other comparison is as before — this tier sits above SPAN, so the same
#: sweeps are skipped as for a substring hit, and an embedded hit still supplies
#: the box wherever it did when nothing better exists.
_TIER_SUBSTRING_EMBEDDED = 3
_TIER_SPAN = 4
# Type-aware equality (date/number/phone parse to the same value) — high
# precision (both sides must parse), ranks just below direct text coverage and
# above partial/token/char fuzzy. This is what bridges format reformatting
# (e.g. 2022-04-04 == 04/04/2022) that text similarity cannot.
_TIER_TYPED = 5
_TIER_PARTIAL = 6
_TIER_FUZZY = 7
# Character-level Levenshtein near-miss (OCR noise) — last resort.
_TIER_LEVENSHTEIN = 8
# A substring match covering less than _PARTIAL_MIN_COVERAGE of the value — a
# FRAGMENT. Ranked last, BELOW the whole-string fuzzy tiers, because a line
# accounting for a tenth of a value is weaker evidence of where that value sits
# than a line resembling the whole of it (#1519).
_TIER_WEAK_PARTIAL = 9

#: Coverage below which a substring match is demoted to ``_TIER_WEAK_PARTIAL``
#: rather than ranked as a real ``_TIER_PARTIAL`` hit (#1519).
#:
#: ``_TIER_PARTIAL`` asked only "is this line a substring of the value", with no
#: floor, and that caused two separate failures. A fragment was **accepted** as the
#: value's location — a box sat on a single digit for a form id, on a single letter
#: for an author name, and on a different author entirely. And because any
#: candidate at all short-circuits the fuzzy and Levenshtein sweeps, a fragment also
#: **suppressed** the match that was right: where OCR misread one character inside a
#: sentence, the 10-character column header ``"IDENTIFIED"`` (29% coverage) won over
#: the correct whole line at 0.9706 similarity, which was never computed.
#:
#: **Demotion, not rejection — and that choice is measured.** Rejecting a sub-floor
#: match looks safer and does more harm than good. Over 265 populated non-list
#: fields across five real documents, 83 ground via ``_TIER_PARTIAL``; of the 62
#: below half coverage, **48 matched a line whose value genuinely is on the page**,
#: spread across several lines (a 3-line address, a specification assembled from
#: consecutive rows), and only 14 were coincidences. Rejecting them all removes 14
#: wrong boxes at the cost of 48 right ones. No absolute-size rule does better: the
#: best tested — at least 2 tokens and 8 characters — reaches exactly break-even, 8
#: removed for 8 lost, because short real content (``"Carbon plate"``, ``"12
#: inches"``) is lexically indistinguishable from short junk (``"UN 173"``). The
#: property that separates them is spatial, not lexical.
#:
#: So a fragment is kept and ranked **last** instead. A whole-string match outranks
#: it where one exists, and it still supplies the only box where one does not.
#: Measured over 50 sections through the full grounding path: **no box is lost**, 10
#: move to a better match, and the rest are unchanged.
#:
#: 0.5 is where the measured distribution splits. At or above it, matches are
#: correct prefixes and suffixes of a value spanning two OCR lines — ``"PO Box 307,
#: Shortland Street"`` for a value continuing ``", Auckland 1140"``. Below it they
#: are fragments, which may still be the best available evidence but should never
#: beat a line resembling the whole value.
_PARTIAL_MIN_COVERAGE = 0.5

# geometry_source tags for the format-bridged tiers (distinct from "ocr" so they
# are auditable). _TIER_TYPED / variant matches -> "ocr-normalized";
# _TIER_LEVENSHTEIN -> "ocr-fuzzy".
_SOURCE_NORMALIZED = "ocr-normalized"
_SOURCE_FUZZY = "ocr-fuzzy"
#: Provenance for a ``_TIER_WEAK_PARTIAL`` box — the matched line covers less than
#: half the value, so this is the weakest evidence of location the ladder emits and
#: is only ever used when nothing better matched (#1519).
#:
#: It exists to make the box **auditable in an artifact**. Without a distinct tag a
#: fragment-based box is recorded as plain ``"ocr"``, identical to a verbatim match,
#: so neither a reviewer nor a later measurement can tell how much of the value a
#: box actually rests on. Counting this value over a corpus is how to answer "how
#: many boxes are standing on a fragment" without re-deriving the tiers.
_SOURCE_WEAK = "ocr-weak"


def _dedup_norm_variants(variants: Optional[List[str]], norm_value: str) -> List[str]:
    """Normalize + dedupe a value's format variants, preserving order.

    ``variants[0]`` (the primary value) stays first so callers can tag any
    non-primary hit as a format-normalized match.
    """
    src = variants if variants else [norm_value]
    seen: set = set()
    out: List[str] = []
    for v in src:
        nv = _normalize(v)
        if nv and nv not in seen:
            seen.add(nv)
            out.append(nv)
    return out


def _ensure_page_indexes(page_data: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """Build (once, cached on ``page_data``) the normalized-line list and the
    EXACT-match index, returning the index.

    Grounding calls into a page once per extracted value (a 300-row × 6-col table
    → 1,800 values). Without caching, every call re-normalized all ~370 lines —
    O(values × lines) work that pushed per-shard grounding of a large table to
    ~60s and toward the shard Lambda's 900s wall. The normalized lines and an
    ``exact-text -> [lines]`` index are computed once and reused; the EXACT index
    turns the common table-cell case (value == one OCR line verbatim) into an O(1)
    dict hit instead of a linear scan + fuzzy ladder. Cached under private keys the
    caller never serializes.
    """
    idx = page_data.get("__exact_index_cache__")
    if idx is not None:
        return idx
    lines = page_data.get("lines")
    norm_lines: List[Tuple[str, Dict[str, Any]]] = (
        [
            (_normalize(line.get("text")), line)
            for line in lines
            if isinstance(line, dict)
        ]
        if isinstance(lines, list)
        else []
    )
    page_data["__norm_lines_cache__"] = norm_lines
    # Token sets per line, cached for the same reason the normalized lines are: the
    # token-overlap sweep runs once per VALUE, so recomputing them there is
    # O(values x lines). That sweep used to be reached rarely — any candidate at all
    # short-circuited it — and #1519 made it reachable for every value whose best
    # match is a fragment, which is a large minority of fields.
    page_data["__norm_tokens_cache__"] = [_tokens(nt) for nt, _ln in norm_lines]
    idx = {}
    for nt, ln in norm_lines:
        if nt:
            idx.setdefault(nt, []).append(ln)
    page_data["__exact_index_cache__"] = idx
    return idx


def _collect_candidates_in_page(
    norm_value: str,
    value_tokens: set,
    page_data: Dict[str, Any],
    page_num: int,
    norm_variants: Optional[List[str]] = None,
    raw_value: Any = None,
    hint: str = HINT_STRING,
) -> List[Tuple[int, Dict[str, Any], str, Optional[float]]]:
    """
    Collect *all* matching OCR lines on one page, each tagged with a precision tier.

    Matching layers (best tier wins; the caller filters to the most precise):
      1. EXACT/SUBSTRING/PARTIAL on the value AND its format variants (``norm_variants``)
         — variants bridge reformatting (e.g. "04/04/2022" for value "2022-04-04").
         A variant hit is tagged ``ocr-normalized``. A substring match covering
         less than ``_PARTIAL_MIN_COVERAGE`` of the value is a FRAGMENT and is
         recorded as ``_TIER_WEAK_PARTIAL`` — still a candidate, but ranked below
         every whole-string tier, and no longer able to short-circuit the sweeps
         below (#1519). Read that constant before changing either. A NUMBER found
         only inside a longer digit run is ``_TIER_SUBSTRING_EMBEDDED``, one place
         below a substring hit on a whole numeric token (#1565).
      2. SPAN — value text reconstructed across consecutive lines.
      3. TYPED — date/number/phone parse to the SAME logical value (``hint``); the
         robust bridge for reformatting that text similarity misses. Tagged
         ``ocr-normalized``.
      4. FUZZY — token overlap (existing).
      5. LEVENSHTEIN — character similarity >= threshold (OCR noise). Tagged
         ``ocr-fuzzy``.

    Returns ``(tier, geometry, geometry_source, ocr_confidence)`` tuples.
    """
    if not norm_value or not page_data.get("geometryAvailable"):
        return []

    lines = page_data.get("lines")
    if not isinstance(lines, list) or not lines:
        return []

    # Variant forms to text-match against (normalized, deduped, non-empty). The
    # primary value is variant[0]; any OTHER variant that hits is a normalized
    # (format-bridged) match and is tagged accordingly.
    norm_variant_list = _dedup_norm_variants(norm_variants, norm_value)

    # Normalized lines + EXACT index are built (once) and cached on page_data.
    _ensure_page_indexes(page_data)
    norm_lines = page_data.get("__norm_lines_cache__") or []

    candidates: List[Tuple[int, Dict[str, Any], str, Optional[float]]] = []
    value_number = _parse_number(raw_value) if hint == HINT_NUMBER else None

    for norm_text, line in norm_lines:
        if not norm_text:
            continue
        best_tier = None
        is_variant_only = True
        for i, nv in enumerate(norm_variant_list):
            if norm_text == nv:
                tier = _TIER_EXACT
            elif nv in norm_text:
                # A number found only inside a longer digit run is demoted one
                # place, not rejected — see _TIER_SUBSTRING_EMBEDDED (#1565). A
                # longer number that is the value with more zeros ("075.00" for
                # 75) is the value, so it keeps the substring tier.
                tier = (
                    _TIER_SUBSTRING_EMBEDDED
                    if hint == HINT_NUMBER
                    and _only_inside_longer_number(nv, norm_text, value_number)
                    else _TIER_SUBSTRING
                )
            elif norm_text in nv:
                # A line covering less than half the value is a fragment. It is
                # DEMOTED rather than rejected — see _TIER_WEAK_PARTIAL — so a
                # whole-string match can outrank it where one exists, and it still
                # supplies the only box where one does not.
                tier = (
                    _TIER_PARTIAL
                    if len(norm_text) >= _PARTIAL_MIN_COVERAGE * len(nv)
                    else _TIER_WEAK_PARTIAL
                )
            else:
                continue
            if best_tier is None or tier < best_tier:
                best_tier = tier
                # primary value (i==0) keeps real "ocr" provenance; a non-primary
                # variant hit is a format-normalized match.
                is_variant_only = i != 0
        if best_tier is not None:
            src = _SOURCE_NORMALIZED if is_variant_only else None
            match = _build_match(line, page_num, best_tier, src)
            if match is not None:
                candidates.append(match)
            continue

        # TYPED equality: same logical date/number/phone despite formatting.
        if hint in (HINT_DATE, HINT_NUMBER, HINT_PHONE) and _type_equal(
            raw_value, line.get("text"), hint
        ):
            match = _build_match(line, page_num, _TIER_TYPED, _SOURCE_NORMALIZED)
            if match is not None:
                candidates.append(match)

    # Tier-aware early-out. The caller keeps only the MOST PRECISE tier that
    # matched, so a pass that can only produce a WORSE tier than what we already
    # have is pure waste. Tiers: EXACT=1 < SUBSTRING=2 < SUBSTRING_EMBEDDED=3 <
    # SPAN=4 < TYPED=5 < PARTIAL=6 < FUZZY=7 < LEVENSHTEIN=8 < WEAK_PARTIAL=9.
    #   • SPAN (4): skip only if we already have EXACT/SUBSTRING/SUBSTRING_EMBEDDED
    #     (≤ SPAN). A mere PARTIAL/TYPED/WEAK_PARTIAL (>SPAN) must NOT skip it — a
    #     multi-line span can still win.
    #   • FUZZY/LEVENSHTEIN (7/8): skip whenever a candidate BETTER THAN FUZZY
    #     exists — those they can never beat. A ``_TIER_WEAK_PARTIAL`` (9)
    #     candidate must fall through to them, since they outrank it; that is the
    #     substance of #1519 and the condition below is written on the tier for
    #     exactly this reason. Skipping these O(lines) sweeps where they cannot
    #     win is still the bulk of the speed-up for real documents.
    best_so_far = min((t for t, *_ in candidates), default=None)
    if best_so_far is None or best_so_far > _TIER_SPAN:
        # Multi-line spans (value == concatenation of consecutive lines).
        candidates.extend(_collect_span_candidates(norm_value, norm_lines, page_num))
        best_so_far = min((t for t, *_ in candidates), default=None)

    if candidates and min(t for t, *_ in candidates) < _TIER_FUZZY:
        # Have a PARTIAL-or-better hit → fuzzy/Levenshtein cannot improve on it.
        #
        # ⚠️ The condition is on the tier, not merely on ``candidates`` being
        # non-empty, and that distinction is the whole of #1519. A bare ``if
        # candidates`` let a FRAGMENT short-circuit these sweeps: a line covering
        # 29% of the value returned here, and the line resembling the whole of it
        # at 0.9706 similarity was never computed, so the box landed on a column
        # header. A ``_TIER_WEAK_PARTIAL`` candidate must therefore fall through —
        # it is kept, and a whole-string match simply outranks it.
        return candidates

    # Token-overlap fuzzy. Line token sets come from the page cache (see
    # _ensure_page_indexes) rather than being rebuilt per value.
    line_tokens = page_data.get("__norm_tokens_cache__") or [
        _tokens(nt) for nt, _ln in norm_lines
    ]
    for (norm_text, line), tokens in zip(norm_lines, line_tokens):
        if not norm_text:
            continue
        if _jaccard(value_tokens, tokens) >= _FUZZY_MATCH_THRESHOLD:
            match = _build_match(line, page_num, _TIER_FUZZY)
            if match is not None:
                candidates.append(match)

    # Character-level Levenshtein near-miss (last resort; OCR noise). The value's
    # stripped length is computed ONCE here: it is the same for every line, and
    # recomputing it inside the loop put two regex passes over the value on every
    # line of the page — in the loop the guard exists to make cheap.
    value_stripped_len = _stripped_len(norm_value)
    for norm_text, line in norm_lines:
        if not norm_text:
            continue
        # O(1) exact ceiling before the O(n·m) dynamic program — see
        # _levenshtein_can_reach. This sweep reaches far more values since the
        # _PARTIAL_MIN_COVERAGE floor stopped fragments from short-circuiting it,
        # and the DP against every line on a page is what made that expensive.
        if not _levenshtein_can_reach(
            norm_value,
            norm_text,
            _LEVENSHTEIN_MATCH_THRESHOLD,
            a_stripped_len=value_stripped_len,
        ):
            continue
        if _levenshtein_sim(norm_value, norm_text) >= _LEVENSHTEIN_MATCH_THRESHOLD:
            match = _build_match(line, page_num, _TIER_LEVENSHTEIN, _SOURCE_FUZZY)
            if match is not None:
                candidates.append(match)

    return candidates


def _collect_span_candidates(
    norm_value: str,
    norm_lines: List[Tuple[str, Dict[str, Any]]],
    page_num: int,
) -> List[Tuple[int, Dict[str, Any], str, Optional[float]]]:
    """Collect multi-line span matches (consecutive lines whose text joins to value)."""
    results: List[Tuple[int, Dict[str, Any], str, Optional[float]]] = []
    n = len(norm_lines)
    for i in range(n):
        if not norm_lines[i][0]:
            continue
        combined = norm_lines[i][0]
        # Single lines are handled by exact/substring; spans start at 2 lines.
        for j in range(i + 1, min(i + _MAX_SPAN_LINES, n)):
            nxt = norm_lines[j][0]
            if not nxt:
                continue
            combined = f"{combined} {nxt}"
            if combined == norm_value:
                span_lines = [norm_lines[k][1] for k in range(i, j + 1)]
                boxes = [b for b in (_line_box(line) for line in span_lines) if b]
                if boxes:
                    source = (
                        "ocr-paragraph"
                        if any(
                            line.get("geometrySource") == "paragraph"
                            for line in span_lines
                        )
                        else "ocr"
                    )
                    confs = [
                        float(c)
                        for line in span_lines
                        if isinstance((c := line.get("confidence")), (int, float))
                    ]
                    ocr_conf = (sum(confs) / len(confs) / 100.0) if confs else None
                    results.append(
                        (
                            _TIER_SPAN,
                            {"boundingBox": _union_box(boxes), "page": page_num},
                            source,
                            ocr_conf,
                        )
                    )
                break
            if len(combined) > len(norm_value):
                break
    return results


def _build_match(
    line: Dict[str, Any],
    page_num: int,
    tier: int,
    source_override: Optional[str] = None,
) -> Optional[Tuple[int, Dict[str, Any], str, Optional[float]]]:
    """Build a candidate tuple from a single OCR line, or None if it lacks geometry.

    ``source_override`` tags format-bridged matches ("ocr-normalized"/"ocr-fuzzy")
    distinctly; otherwise provenance is "ocr"/"ocr-paragraph" from the line.

    A ``_TIER_WEAK_PARTIAL`` match is always tagged ``"ocr-weak"``, overriding both,
    because how little of the value the box rests on is the more important fact
    about it than which surface form matched — and it is the only way to tell such
    a box from a verbatim one in a stored artifact.
    """
    box = _line_box(line)
    if box is None:
        return None
    if tier == _TIER_WEAK_PARTIAL:
        source = _SOURCE_WEAK
    elif source_override is not None:
        source = source_override
    else:
        source = "ocr-paragraph" if line.get("geometrySource") == "paragraph" else "ocr"
    conf = line.get("confidence")
    ocr_conf = conf / 100.0 if isinstance(conf, (int, float)) else None
    return (tier, {"boundingBox": box, "page": page_num}, source, ocr_conf)


def _box_center(geometry: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """Center (cx, cy) of a ``{boundingBox:{...}}`` geometry, or None."""
    bb = geometry.get("boundingBox") if isinstance(geometry, dict) else None
    if not isinstance(bb, dict):
        return None
    try:
        return (bb["left"] + bb["width"] / 2.0, bb["top"] + bb["height"] / 2.0)
    except (KeyError, TypeError):
        return None


def _ref_from_llm_geometry(
    preferred_geometry: Optional[Dict[str, Any]],
) -> Optional[Tuple[int, float, float]]:
    """Extract ``(page, cx, cy)`` from the LLM-estimated geometry, if usable."""
    if not isinstance(preferred_geometry, dict):
        return None
    page_val = preferred_geometry.get("page")
    if isinstance(page_val, str) and page_val.isdigit():
        page_val = int(page_val)
    if not isinstance(page_val, int):
        return None
    center = _box_center(preferred_geometry)
    if center is None:
        return None
    return (page_val, center[0], center[1])


def _reading_order_key(
    candidate: Tuple[int, Dict[str, Any], str, Optional[float]],
) -> Tuple[int, float, float]:
    """Sort key placing candidates in document reading order: page, top, left."""
    geometry = candidate[1]
    page = geometry.get("page", 0)
    bb = geometry.get("boundingBox") if isinstance(geometry, dict) else None
    if isinstance(bb, dict):
        return (page, bb.get("top", 0.0), bb.get("left", 0.0))
    return (page, 0.0, 0.0)


def match_value_to_geometry(
    value: Any,
    page_data_by_page: Dict[int, Dict[str, Any]],
    preferred_geometry: Optional[Dict[str, Any]] = None,
    occurrence_index: Optional[int] = None,
    schema_hint: Optional[Dict[str, Any]] = None,
) -> Optional[Tuple[Dict[str, Any], str, Optional[float]]]:
    """
    Match an extracted value to a real OCR line across the section's pages.

    Collects every candidate line across all pages, keeps only the best (most
    precise) tier, and resolves ties between equally-good text matches (e.g. the
    same transaction amount on several rows). Two disambiguation strategies:

    - **Row order** (``occurrence_index`` set — the ``ocr_only`` geometry mode):
      candidates are sorted into document reading order (page, then top, then
      left) and the one at ``occurrence_index`` is chosen. The i-th assessed list
      item is the i-th extracted row, and OCR lines read top-to-bottom, so the
      i-th occurrence of a repeated value is that row's. Needs no LLM box.
    - **Spatial proximity** (``preferred_geometry`` set, ``occurrence_index`` None —
      the ``llm_grounded`` mode): the candidate whose center is
      nearest the LLM-estimated box wins.

    Safe fallback: if multiple candidates remain and there is no usable
    disambiguator (no ``occurrence_index`` and no usable LLM reference), the match
    is treated as ambiguous and None is returned.

    Args:
        value: The extracted attribute value (scalar; coerced to string).
        page_data_by_page: Mapping of 1-indexed page number -> pageData dict.
        preferred_geometry: The LLM-estimated ``{boundingBox, page}`` reference
            (used only for spatial disambiguation; ignored when occurrence_index set).
        occurrence_index: 0-based position of this value among repeated occurrences,
            used for row-order disambiguation. Out-of-range clamps to the last.

    Returns:
        ``(geometry, geometry_source, ocr_confidence)`` where ``geometry`` is
        ``{"boundingBox": {...0-1...}, "page": <int>}``, or None when no confident,
        unambiguous match is found.
    """
    if value is None or not page_data_by_page:
        return None

    norm_value = _normalize(value)
    if not norm_value:
        return None
    value_tokens = _tokens(norm_value)

    # Resolve the logical type (schema hint first, else infer) and build the
    # format variants to text-match against. Bridges reformatting like
    # "2022-04-04" (value) vs "04/04/2022" (document).
    hint = _resolve_hint(value, schema_hint)
    variants = _value_variants(value, hint)

    ref = _ref_from_llm_geometry(preferred_geometry)

    norm_variant_list = _dedup_norm_variants(variants, norm_value)

    # FAST PATH — EXACT index lookup across ALL pages first. EXACT (tier 1) is the
    # best possible tier, so if any page's O(1) index has a verbatim hit for a
    # variant we take those candidates and NEVER run the per-page O(lines) scan +
    # fuzzy/Levenshtein sweeps. This is what keeps grounding a 300-row table fast:
    # every cell value equals its OCR line verbatim, so it's a dict hit, not a scan
    # over ~1,800 lines × the full fuzzy ladder. Falls through to the full scan only
    # when nothing matched exactly (reformatting / OCR noise).
    candidates: List[Tuple[int, Dict[str, Any], str, Optional[float]]] = []
    for page_num in sorted(page_data_by_page):
        page_data = page_data_by_page[page_num]
        if not isinstance(page_data, dict) or not page_data.get("geometryAvailable"):
            continue
        idx = _ensure_page_indexes(page_data)
        for i, nv in enumerate(norm_variant_list):
            for ln in idx.get(nv, ()):
                m = _build_match(
                    ln, page_num, _TIER_EXACT, None if i == 0 else _SOURCE_NORMALIZED
                )
                if m is not None:
                    candidates.append(m)
    if candidates:
        best_tier = _TIER_EXACT  # by construction
    else:
        # Full (slower) collection across all pages: substring/span/typed/fuzzy.
        for page_num in sorted(page_data_by_page):
            candidates.extend(
                _collect_candidates_in_page(
                    norm_value,
                    value_tokens,
                    page_data_by_page[page_num],
                    page_num,
                    norm_variants=variants,
                    raw_value=value,
                    hint=hint,
                )
            )

    if not candidates:
        return None

    # Keep only the most precise tier that matched (exact beats substring beats
    # span beats fuzzy), so a high-precision hit is never displaced by fuzzy noise.
    best_tier = min(tier for tier, *_ in candidates)
    best = [c for c in candidates if c[0] == best_tier]

    # At the best matching tier the tie is about provenance rather than precision, so
    # prefer the line carrying the value as extracted over a format-bridged rendering
    # of it. Without this the tie-break below is positional, and collapsing a
    # separator can promote a variant to EXACT alongside the verbatim hit: a
    # ``"4-15-11"`` whose own line sits on page 2 is then grounded on page 1's
    # ``"APR 15 2011"`` stamp, which reading order reaches first (#1520).
    #
    # ⚠️ **Never when a row mapping is in play.** With ``occurrence_index`` set, row
    # *i* is the *i*-th candidate in reading order, so the position in this list IS
    # the row correspondence — and filtering by provenance destroys it. A column
    # where one occurrence is rendered verbatim and the others only match through a
    # variant then narrows to the single verbatim candidate, the ``len(best) == 1``
    # return below fires, and **every row is handed the same box**; with several
    # survivors, row *i* indexes a pool that no longer contains row *i*. Row order
    # already disambiguates those rows, so the preference has nothing to add there —
    # it exists for the scalar case, which passes no index.
    #
    # ⚠️ **Nor when a spatial signal exists.** Under ``llm_grounded`` the model's own
    # box arrives as ``preferred_geometry`` and the branch below picks the candidate
    # nearest it. Filtering first could discard a separator-bridged candidate sitting
    # exactly where the model said the value was, in favour of a verbatim one
    # elsewhere on the page — which is the opposite of what that signal is for. So the
    # preference runs only when neither disambiguator is present, which is the
    # ``ocr_only`` scalar case it was measured on.
    #
    # ⚠️ **Bounded by what it is NOT asked to decide.** For a number the primary
    # rendering is arbitrary — ``str(6.0)`` is ``"6.0"``, a substring of
    # ``"126.00 total"`` — so below EXACT this preference can favour a coincidence
    # over the line that holds the value. That hazard predates this change and is
    # not closed here: a bare number matching inside a longer one is the real defect,
    # and two attempts to bound it from this side were each measured as net-negative
    # on an 18,177-section corpus (an EXACT-tier gate cost 15 correct rate boxes; a
    # token-boundary rule cost 73 account numbers the model had transcribed one digit
    # short, where the box was on the right field and the highlight is what would
    # have caught the error). It wants its own change and its own measurement.
    if occurrence_index is None and ref is None:
        verbatim_at_tier = [c for c in best if c[2] != _SOURCE_NORMALIZED]
        if verbatim_at_tier:
            best = verbatim_at_tier

    if len(best) == 1:
        _, geometry, source, ocr_conf = best[0]
        return (geometry, source, ocr_conf)

    # Ambiguous: multiple equally-good text matches (e.g. a value repeated across
    # table rows).
    #
    # Row-order disambiguation (ocr_only mode): pick the occurrence_index-th match
    # in document reading order. This is deterministic and needs no LLM box — the
    # i-th assessed row maps to the i-th occurrence top-to-bottom.
    if occurrence_index is not None:
        ordered = sorted(best, key=_reading_order_key)
        idx = occurrence_index if occurrence_index < len(ordered) else len(ordered) - 1
        if idx < 0:
            idx = 0
        _, geometry, source, ocr_conf = ordered[idx]
        return (geometry, source, ocr_conf)

    # Spatial disambiguation (llm_grounded mode): proximity to the
    # LLM-estimated box, which sits at a roughly-correct distinct position per
    # occurrence. Prefer candidates on the LLM's page; among those, nearest center.
    if ref is not None:
        ref_page, ref_cx, ref_cy = ref
        on_ref_page = [c for c in best if c[1].get("page") == ref_page]
        pool = on_ref_page or best

        def _distance(c: Tuple[int, Dict[str, Any], str, Optional[float]]) -> float:
            center = _box_center(c[1])
            if center is None:
                return float("inf")
            return (center[0] - ref_cx) ** 2 + (center[1] - ref_cy) ** 2

        nearest = min(pool, key=_distance)
        if _distance(nearest) != float("inf"):
            _, geometry, source, ocr_conf = nearest
            return (geometry, source, ocr_conf)

    # A usable LLM reference was supplied (llm_grounded mode) but
    # couldn't disambiguate -> stay ambiguous and KEEP the LLM box (return None),
    # preserving prior behavior.
    if ref is not None:
        logger.debug(
            "Ambiguous OCR grounding for '%s' (%d candidates, LLM ref didn't "
            "resolve); keeping LLM box",
            norm_value,
            len(best),
        )
        return None

    # No disambiguator at all (ocr_only scalar field whose value legitimately
    # appears on several lines, e.g. a statement date printed in multiple places).
    # A scalar's value is identical wherever it appears, so the FIRST occurrence in
    # reading order is a sound, deterministic choice — better than no geometry.
    # (List items never reach here: they always pass an occurrence_index.)
    first = sorted(best, key=_reading_order_key)[0]
    _, geometry, source, ocr_conf = first
    return (geometry, source, ocr_conf)


def _field_schema_for(schema_node: Any, key: str) -> Optional[Dict[str, Any]]:
    """Return the JSON-Schema fragment for child ``key`` of ``schema_node``, if any."""
    if not isinstance(schema_node, dict):
        return None
    props = schema_node.get("properties")
    if isinstance(props, dict) and key in props and isinstance(props[key], dict):
        return props[key]
    return None


def _item_schema_of(schema_node: Any) -> Optional[Dict[str, Any]]:
    """Return the array ``items`` schema fragment of ``schema_node``, if any."""
    if isinstance(schema_node, dict):
        items = schema_node.get("items")
        if isinstance(items, dict):
            return items
    return None


def _ground_node(
    assessment_node: Any,
    extraction_node: Any,
    page_data_by_page: Dict[int, Dict[str, Any]],
    geometry_mode: str = "llm_grounded",
    occurrence_index: Optional[int] = None,
    schema_node: Optional[Dict[str, Any]] = None,
    skip_grounded: bool = False,
) -> None:
    """
    Recursively walk an assessment subtree, grounding leaf geometries in place.

    The recursion mirrors ``_extract_geometry_from_assessment``: a dict with a
    ``confidence`` key is a leaf assessment; otherwise it is a group; lists are
    walked element-wise alongside the parallel extraction list. ``occurrence_index``
    is the position of the current item within its parent list (None at the top
    level / for scalars), used for ocr_only row-order disambiguation of repeated
    values. ``schema_node`` is the JSON-Schema fragment for the current node (its
    ``format``/``type`` give the format-matching hint), descended in parallel.
    ``skip_grounded`` leaves already-grounded leaves untouched (see
    :func:`ground_assessment_geometry`).
    """
    if isinstance(assessment_node, dict):
        if "confidence" in assessment_node:
            if skip_grounded and assessment_node.get("geometry_source"):
                return
            _ground_leaf(
                assessment_node,
                extraction_node,
                page_data_by_page,
                geometry_mode,
                occurrence_index,
                schema_node,
            )
            return
        # Group: descend into each child alongside the matching extraction value.
        #
        # Two extraction shapes occur here:
        #  - True group: the extraction value is a dict, so each child key maps to a
        #    sub-value (e.g. CompanyAddress -> {State, ZipCode}).
        #  - Decomposed string: the schema field is a plain string, but the assessment
        #    LLM split it into sub-keyed assessments where each *key* is a fragment of
        #    the value (e.g. "Insurance Company": {"Fake Insurance Co": {...},
        #    "650 Davis Street": {...}}). The extraction value is then a string, not a
        #    dict. In that case the sub-key name itself is the text to ground, but only
        #    when it is actually part of the extracted string — otherwise a genuine
        #    group with a missing extraction value could spuriously match label text.
        extraction_is_str = isinstance(extraction_node, str)
        norm_parent = _normalize(extraction_node) if extraction_is_str else ""
        for key, child in assessment_node.items():
            if isinstance(extraction_node, dict):
                child_extraction = extraction_node.get(key)
            elif (
                extraction_is_str and _normalize(key) and _normalize(key) in norm_parent
            ):
                # Decomposed-string fragment: match the fragment key text.
                child_extraction = key
            else:
                child_extraction = None
            # Children of a group inherit this node's occurrence_index (a group
            # field inside list row i still belongs to row i's occurrence) and
            # descend into the matching schema property fragment.
            _ground_node(
                child,
                child_extraction,
                page_data_by_page,
                geometry_mode,
                occurrence_index,
                _field_schema_for(schema_node, key),
                skip_grounded,
            )
    elif isinstance(assessment_node, list):
        item_schema = _item_schema_of(schema_node)
        for idx, item in enumerate(assessment_node):
            item_extraction = (
                extraction_node[idx]
                if isinstance(extraction_node, list) and idx < len(extraction_node)
                else None
            )
            # The list index IS the occurrence index for row-order disambiguation;
            # list items share the array's ``items`` schema fragment.
            _ground_node(
                item,
                item_extraction,
                page_data_by_page,
                geometry_mode,
                idx,
                item_schema,
                skip_grounded,
            )


def _ground_leaf(
    leaf: Dict[str, Any],
    value: Any,
    page_data_by_page: Dict[int, Dict[str, Any]],
    geometry_mode: str = "llm_grounded",
    occurrence_index: Optional[int] = None,
    schema_node: Optional[Dict[str, Any]] = None,
) -> None:
    """Ground a single leaf assessment (has a ``confidence`` key) in place."""
    existing_geometry = leaf.get("geometry")

    if geometry_mode == "ocr_only":
        # Derive geometry purely from OCR value-matching; the LLM box (if any) is
        # NOT used as a reference — repeated values are disambiguated by row order
        # via occurrence_index. Match found -> real OCR box; no match -> no box.
        match = match_value_to_geometry(
            value, page_data_by_page, None, occurrence_index, schema_node
        )
        if match is None:
            # No OCR match: drop any stray LLM-provided box so we never emit
            # hallucinated coordinates in ocr_only mode.
            leaf.pop("geometry", None)
            leaf.pop("geometry_source", None)
            return
        geometry, source, ocr_conf = match
        leaf["geometry"] = [geometry]
        leaf["geometry_source"] = source
        if ocr_conf is not None:
            leaf["ocr_confidence"] = round(ocr_conf, 4)
        return

    # llm_grounded: the existing (LLM-estimated) box doubles as
    # the spatial reference used to disambiguate repeated values across table rows.
    preferred_geometry: Optional[Dict[str, Any]] = None
    if (
        isinstance(existing_geometry, list)
        and existing_geometry
        and isinstance(existing_geometry[0], dict)
    ):
        preferred_geometry = existing_geometry[0]

    match = match_value_to_geometry(
        value, page_data_by_page, preferred_geometry, None, schema_node
    )
    if match is None:
        # No OCR match -> keep the LLM box. Tag provenance only if a box exists.
        if existing_geometry:
            leaf["geometry_source"] = "llm"
        return

    geometry, source, ocr_conf = match
    leaf["geometry"] = [geometry]
    leaf["geometry_source"] = source
    if ocr_conf is not None:
        leaf["ocr_confidence"] = round(ocr_conf, 4)


def ground_assessment_geometry(
    enhanced_assessment: Dict[str, Any],
    extraction_results: Dict[str, Any],
    page_data_by_page: Dict[int, Dict[str, Any]],
    geometry_mode: str = "llm_grounded",
    class_schema: Optional[Dict[str, Any]] = None,
    skip_grounded: bool = False,
) -> Dict[str, Any]:
    """
    Produce field geometry from OCR (and optionally the LLM box), in place.

    Mutates and returns ``enhanced_assessment``. ``geometry_mode``:
    - ``"ocr_only"`` (default in AssessmentConfig): geometry is derived purely by
      matching each extracted value to OCR lines; the LLM box is ignored and a
      field with no OCR match is left with no geometry. Repeated values are
      disambiguated by row order. When ``page_data_by_page`` is empty this leaves
      fields without geometry (nothing to ground against).
    - ``"llm_grounded"``: refine LLM-estimated boxes with OCR geometry,
      falling back to the LLM box when unmatched (near no-op when no OCR data).

    Args:
        enhanced_assessment: The per-field assessment dict (the object that becomes
            ``explainability_info[0]``).
        extraction_results: The original ``inference_result`` values, used to look up
            each field's extracted value for matching.
        page_data_by_page: Mapping of 1-indexed page number -> pageData dict, from
            ``load_page_ocr_data``.
        geometry_mode: ``"ocr_only"``, ``"llm_grounded"``, ``"llm"``, or ``"off"``.
        skip_grounded: When True, a leaf that already carries a ``geometry_source``
            is left untouched. This makes a post-merge grounding pass a near-instant
            no-op when the rows were already grounded per-shard (each shard grounds
            its own rows against its own pages), while still grounding any residual
            leaves (e.g. reconcile-padded placeholder rows, or the non-sharded
            single-agent path that grounds only once at the end).

    Returns:
        The same ``enhanced_assessment`` dict, grounded in place.
    """
    try:
        for attr_name, attr_assessment in enhanced_assessment.items():
            extraction_value = (
                extraction_results.get(attr_name)
                if isinstance(extraction_results, dict)
                else None
            )
            _ground_node(
                attr_assessment,
                extraction_value,
                page_data_by_page,
                geometry_mode,
                None,
                _field_schema_for(class_schema, attr_name),
                skip_grounded,
            )
    except Exception as e:
        # Grounding is best-effort enrichment; never fail assessment over it.
        logger.warning(f"OCR geometry grounding failed; keeping LLM boxes: {e}")
    return enhanced_assessment
