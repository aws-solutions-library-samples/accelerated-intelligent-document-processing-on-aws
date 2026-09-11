# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
GENAIIDP-cohere-parse-hook: Lambda Hook that calls the hosted Cohere Parse API.

This Lambda function receives a Converse API-compatible payload from the
GenAI IDP Accelerator's LambdaHook feature and forwards each page image to the
hosted Cohere Parse API (https://api.cohere.com/v2/parse) for high-quality OCR.

Cohere Parse ("parse-v5.0") is a vision language model that converts document
images into Markdown, with tables emitted as HTML and bounding boxes for tables
and figures. This hook is fully serverless (HTTPS API + API key) — no SageMaker
endpoint or GPU instance is required.

How this hook differs from the Mistral OCR hook
-----------------------------------------------
Cohere Parse returns **no confidence scores at all** (documented), so this hook
cannot feed OCR confidence into Assessment the way the Mistral hook does. It
does return geometry, but only for **tables and figures** — never for text. So:

1. It requests ``output_format="blocks"``, which yields reading-ordered text
   blocks plus table/figure blocks carrying ``bounding_box_normalized``.
2. It converts Parse's **HTML tables into Markdown pipe tables**. This matters:
   the accelerator's deterministic table-parsing tool (agentic extraction) keys
   on Markdown pipe tables, and HTML tables would silently fall back to
   pure-LLM extraction, losing the completeness guarantee that matters most on
   large tabular documents.
3. It emits Amazon Textract-format blocks under ``textractBlocks`` with
   ``Geometry`` but **no** ``Confidence``. Lines derived from one table/figure
   share that element's box, which the IDP OCR service already recognizes as
   paragraph-level geometry (``geometrySource: "paragraph"``), so table and
   figure highlighting works in the UI Visual Editor with no core changes.
4. It reports per-page metering (``pages``) so cost tracking works.
5. It retries throttled (429) and 5xx responses with exponential backoff —
   Cohere's Parse rate limit is a flat 500 requests/minute for both trial and
   production keys, which IDP concurrency can reach.

The function:
1. Downloads page images from S3 (sent as S3 references by the accelerator).
2. Submits each image to the Cohere Parse API as a base64 data URI.
3. Converts HTML tables to Markdown and translates blocks to Textract format.
4. Maps everything back to a Converse API-compatible response for the pipeline.

Environment variables:
  COHERE_API_KEY      - (Required) Cohere API key (Bearer token).
  COHERE_API_URL      - Parse endpoint (default:
                        https://api.cohere.com/v2/parse)
  COHERE_PARSE_MODEL  - Parse model id (default: parse-v5.0)
  OUTPUT_FORMAT       - "blocks" or "markdown" (default: blocks). "blocks" is
                        required for table/figure geometry.
  CONVERT_HTML_TABLES - "true"/"false" — convert Parse's HTML tables into
                        Markdown pipe tables (default: true)
  MAX_RETRIES         - Retry attempts for 429/5xx responses (default: 4)
  RETRY_BASE_DELAY    - Initial backoff in seconds, doubled per attempt
                        (default: 1)
  REQUEST_TIMEOUT     - Per-request timeout in seconds (default: 120)
  LOG_LEVEL           - Logging level (default: INFO)
"""

import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Initialize AWS clients
s3_client = boto3.client("s3")

# Configuration from environment variables
COHERE_API_KEY = os.environ.get("COHERE_API_KEY", "")
COHERE_API_URL = os.environ.get("COHERE_API_URL", "https://api.cohere.com/v2/parse")
COHERE_PARSE_MODEL = os.environ.get("COHERE_PARSE_MODEL", "parse-v5.0")
OUTPUT_FORMAT = os.environ.get("OUTPUT_FORMAT", "blocks").lower()
CONVERT_HTML_TABLES = os.environ.get("CONVERT_HTML_TABLES", "true").lower() == "true"
MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "4"))
RETRY_BASE_DELAY = float(os.environ.get("RETRY_BASE_DELAY", "1"))
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "120"))

# Image format to MIME type mapping (for building the data URI)
IMAGE_MIME_TYPES = {
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "tiff": "image/tiff",
    "tif": "image/tiff",
}


def download_image_from_s3(s3_uri: str) -> bytes:
    """Download image bytes from an S3 URI."""
    parts = s3_uri.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    key = parts[1]
    response = s3_client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read()


def extract_images_from_messages(messages: list) -> list[dict]:
    """
    Extract images from Converse API messages.

    Args:
        messages: List of Converse API message objects

    Returns:
        List of dicts with 'bytes' and 'format' keys
    """
    images = []
    for message in messages:
        for item in message.get("content", []):
            if "image" not in item:
                continue
            source = item["image"].get("source", {})
            img_format = item["image"].get("format", "jpeg")
            if "s3Location" in source:
                s3_uri = source["s3Location"]["uri"]
                try:
                    img_bytes = download_image_from_s3(s3_uri)
                    images.append({"bytes": img_bytes, "format": img_format})
                    logger.info(
                        f"Downloaded image from S3: {s3_uri} ({len(img_bytes)} bytes)"
                    )
                except Exception as e:
                    # Do not swallow this. The accelerator sends one page image
                    # per invocation, so continuing here would return empty text
                    # with pages=0 and no error — the pipeline would record a
                    # blank page as a successful OCR and no retry layer would
                    # engage. Failing loudly is what gets the page retried.
                    logger.error(
                        f"Failed to download image from {s3_uri}: {e}", exc_info=True
                    )
                    raise
            elif "bytes" in source:
                images.append({"bytes": source["bytes"], "format": img_format})
    return images


def call_cohere_parse(image_bytes: bytes, image_format: str) -> dict:
    """
    Submit a single image to the hosted Cohere Parse API.

    Cohere Parse accepts one image per call (``document.type: "image_url"``
    only — PDFs and file uploads are not supported), which lines up exactly with
    the per-page images the accelerator sends.

    Retries 429 (throttling) and 5xx responses with exponential backoff,
    honoring a ``Retry-After`` header when the service supplies one.

    Args:
        image_bytes: Raw image bytes
        image_format: Image format string (e.g. 'jpeg', 'png')

    Returns:
        The parsed JSON Parse response.

    Raises:
        ValueError: If the API key is not configured.
        urllib.error.HTTPError: If the request still fails after all retries.
    """
    if not COHERE_API_KEY:
        raise ValueError(
            "COHERE_API_KEY environment variable is required. "
            "Get your API key from https://dashboard.cohere.com/api-keys"
        )

    # The API key travels in an Authorization header to whatever COHERE_API_URL
    # names, so refuse anything but HTTPS: a misconfigured (or tampered) endpoint
    # would otherwise send the key in clear text, or to another host entirely.
    if not COHERE_API_URL.lower().startswith("https://"):
        raise ValueError(
            f"COHERE_API_URL must be an https:// URL, got: {COHERE_API_URL!r}"
        )

    mime_type = IMAGE_MIME_TYPES.get(image_format.lower(), "image/jpeg")
    b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_uri = f"data:{mime_type};base64,{b64}"

    payload = {
        "model": COHERE_PARSE_MODEL,
        "document": {"type": "image_url", "image_url": data_uri},
        "output_format": OUTPUT_FORMAT,
    }

    req = urllib.request.Request(
        COHERE_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {COHERE_API_KEY}",
            "X-Client-Name": "GENAIIDP-cohere-parse-hook",
            "User-Agent": "GENAIIDP-cohere-parse-hook/1.0",
        },
        method="POST",
    )

    logger.info(
        f"Submitting image to Cohere Parse ({len(image_bytes)} bytes, "
        f"model={COHERE_PARSE_MODEL}, output_format={OUTPUT_FORMAT})"
    )

    delay = RETRY_BASE_DELAY
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(  # nosec B310 - COHERE_API_URL env var (https default), not request input
                req, timeout=REQUEST_TIMEOUT
            ) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            retryable = e.code == 429 or e.code >= 500
            if not retryable or attempt == MAX_RETRIES:
                # Read the body for a useful error message; Cohere returns
                # {"message": "..."} on failures.
                body = ""
                try:
                    body = e.read().decode("utf-8")[:500]
                except Exception:  # nosec B110 - best-effort diagnostics only
                    pass
                logger.error(f"Cohere Parse request failed (HTTP {e.code}): {body}")
                raise
            wait = _retry_after_seconds(e) or delay
            logger.warning(
                f"Cohere Parse HTTP {e.code} (attempt {attempt + 1}/"
                f"{MAX_RETRIES + 1}); retrying in {wait:.1f}s"
            )
            time.sleep(wait)
            delay *= 2
        # A socket read timeout raises a bare TimeoutError, which is NOT a
        # URLError subclass — and with pages taking 50-90s against a 120s
        # REQUEST_TIMEOUT it is the likeliest transient failure of all, so it
        # must be retried rather than propagate on the first attempt. OSError
        # covers the reset/broken-pipe family; a truncated body surfaces as
        # JSONDecodeError and is equally worth one more try.
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            json.JSONDecodeError,
        ) as e:
            if attempt == MAX_RETRIES:
                logger.error(f"Cohere Parse request failed: {type(e).__name__}: {e}")
                raise
            logger.warning(
                f"Cohere Parse {type(e).__name__} (attempt {attempt + 1}/"
                f"{MAX_RETRIES + 1}): {e}; retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            delay *= 2

    # Unreachable: the loop either returns or raises.
    raise RuntimeError("Cohere Parse request exhausted retries without a result")


def _retry_after_seconds(error: urllib.error.HTTPError) -> float | None:
    """Parse a numeric Retry-After header, if present and sane."""
    try:
        value = float(error.headers.get("Retry-After", ""))
    except (AttributeError, TypeError, ValueError):
        return None
    # Ignore absurd values so a misbehaving header can't stall the Lambda.
    return value if 0 < value <= 60 else None


# ---------------------------------------------------------------------------
# HTML tables -> Markdown pipe tables
# ---------------------------------------------------------------------------
#
# Cohere Parse emits tables as HTML. The accelerator's deterministic table
# parser (agentic extraction) recognizes Markdown pipe tables, so converting
# here keeps that path working — otherwise every table falls back to pure-LLM
# extraction and loses the row-completeness guarantee.
#
# Scope is deliberately narrow:
#
# * Header detection follows `<thead>` first, then `<th>`. Cohere Parse marks
#   its header row with `<thead>` containing plain `<td>` cells (verified
#   against the live API) — so keying on `<th>` alone would leave the real
#   header sitting in the body under a blank header row, and the deterministic
#   table parser would then read every column name as an empty string.
# * `colspan` is expanded to keep columns aligned, placing the text in the
#   first spanned column and leaving the rest empty. Repeating the text across
#   the span would show the extraction model the same label two or three times
#   as if they were distinct column values.
# * `rowspan` makes the table unrepresentable, so it falls back to raw HTML.
#   Markdown has no vertical span: the rows a spanning cell covers each carry
#   one fewer cell, so every value after it shifts a column left and lands
#   under the wrong header — silently, since the row still has a plausible
#   cell count. Wrong values filed under the wrong field are worse than an
#   unparsed table, which at least leaves the LLM to read the HTML.
# * Nested tables cannot be represented in Markdown either, so a table
#   containing another table also falls back to its original HTML.
# * A pipe inside a cell is replaced with U+2502 (│) rather than escaped as
#   `\|`. The downstream deterministic table parser splits rows on a bare `|`
#   and does not honour the escape, so `x\|y` becomes two cells: the row gains
#   a column, the last value is truncated away and the rest are misattributed.
#   One visually identical codepoint is a far smaller loss.


# Block-level tags inside a cell imply a visual break, so they must not run two
# values together ("<div>one</div><div>two</div>" is "one two", not "onetwo").
_CELL_BREAK_TAGS = frozenset(
    {"br", "div", "p", "li", "ul", "ol", "tr", "span", "h1", "h2", "h3", "h4"}
)


class _HTMLTableParser(HTMLParser):
    """Collect the cell text of every top-level ``<table>`` in an HTML string."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        # One entry per table: {"rows": [[cell, ...], ...],
        #                       "row_is_header": [bool, ...], "caption": str,
        #                       "unrepresentable": bool}
        self.tables: list[dict] = []
        self._table_depth = 0
        self._in_thead = False
        self._cell_parts: list[str] | None = None
        self._caption_parts: list[str] | None = None
        self._colspan = 1

    # -- structure ---------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._table_depth += 1
            if self._table_depth == 1:
                self.tables.append(
                    {
                        "rows": [],
                        "row_is_header": [],
                        "caption": "",
                        "unrepresentable": False,
                    }
                )
            else:
                # Nested table: Markdown cannot express it.
                self._mark_unrepresentable()
            return

        if self._table_depth != 1 or not self.tables:
            return

        if tag == "thead":
            self._in_thead = True
        elif tag == "caption":
            self._caption_parts = []
        elif tag == "tr":
            self.tables[-1]["rows"].append([])
            self.tables[-1]["row_is_header"].append(self._in_thead)
        elif tag in ("td", "th"):
            attrs_map = dict(attrs)
            # A vertical span shifts every later cell in the covered rows into
            # the wrong column, so refuse the whole table rather than emit
            # plausible-looking rows with values under the wrong headers.
            if _positive_int(attrs_map.get("rowspan"), default=1) > 1:
                self._mark_unrepresentable()
            self._cell_parts = []
            self._colspan = _positive_int(attrs_map.get("colspan"), default=1)
            if tag == "th":
                self._mark_current_row_as_header()
        elif tag in _CELL_BREAK_TAGS and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "table":
            self._table_depth = max(0, self._table_depth - 1)
            return

        if self._table_depth != 1 or not self.tables:
            return

        if tag == "thead":
            self._in_thead = False
        elif tag == "caption":
            if self._caption_parts is not None:
                self.tables[-1]["caption"] = _clean_cell("".join(self._caption_parts))
                self._caption_parts = None
        elif tag in ("td", "th"):
            self._flush_cell()
        elif tag in _CELL_BREAK_TAGS and self._cell_parts is not None:
            self._cell_parts.append(" ")

    def handle_data(self, data):
        if self._table_depth != 1:
            return
        if self._caption_parts is not None:
            self._caption_parts.append(data)
        elif self._cell_parts is not None:
            self._cell_parts.append(data)

    def close(self):
        # Malformed HTML can end without closing its last cell; keep its text
        # rather than dropping the value silently.
        super().close()
        self._flush_cell()

    def _flush_cell(self) -> None:
        """Commit the cell being built, if any, to the current row."""
        if self._cell_parts is None or not self.tables:
            return
        text = _clean_cell("".join(self._cell_parts))
        table = self.tables[-1]
        if not table["rows"]:  # cell outside any <tr>
            table["rows"].append([])
            table["row_is_header"].append(self._in_thead)
        # Spanned columns keep the text in the first one only.
        table["rows"][-1].extend([text] + [""] * (self._colspan - 1))
        self._cell_parts = None
        self._colspan = 1

    def _mark_unrepresentable(self) -> None:
        if self.tables:
            self.tables[-1]["unrepresentable"] = True

    def _mark_current_row_as_header(self) -> None:
        """Flag the row being built as a header row (a `<th>` was seen in it)."""
        table = self.tables[-1]
        if table["row_is_header"]:
            table["row_is_header"][-1] = True


def _positive_int(value, default: int = 1) -> int:
    """Parse a positive int attribute, clamping to a sane range."""
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    # Cap the span so a malformed attribute cannot explode the row width.
    return parsed if 1 <= parsed <= 64 else default


def _clean_cell(text: str) -> str:
    """
    Collapse whitespace and make a cell safe to sit inside a Markdown row.

    A literal pipe becomes U+2502 (│) rather than an escaped ``\\|``: the
    downstream deterministic table parser splits on a bare ``|`` and does not
    honour the escape, so escaping would add a phantom column, truncate the
    row's last value and misattribute the rest. Substituting a look-alike
    codepoint keeps the row shape and the reading.
    """
    return " ".join(text.split()).replace("|", "│")


def _rows_to_markdown(rows: list[list[str]], row_is_header: list[bool]) -> str:
    """Render parsed rows as a Markdown pipe table."""
    # Drop empty rows, keeping each surviving row paired with its header flag.
    kept = [
        (row, is_header)
        for row, is_header in zip(rows, row_is_header + [False] * len(rows))
        if row
    ]
    if not kept:
        return ""

    width = max(len(row) for row, _ in kept)
    padded = [(row + [""] * (width - len(row)), is_header) for row, is_header in kept]

    if padded[0][1]:
        # The first row is the header row. Any later header-ish row stays in the
        # body so no cell is dropped.
        header = padded[0][0]
        body = [row for row, _ in padded[1:]]
    else:
        # Markdown requires a header row, and it has to be the first one. With
        # no leading <thead>/<th>, synthesize an empty header: promoting a later
        # header row would reorder the table, printing the rows above it after
        # it. An empty header costs nothing — the deterministic table parser
        # renames blank columns to _unnamed_N.
        header, body = [""] * width, [row for row, _ in padded]

    lines = [
        "| " + " | ".join(header) + " |",
        "|" + "|".join(["---"] * width) + "|",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in body)
    return "\n".join(lines)


def html_table_to_markdown(html: str) -> str:
    """
    Convert every ``<table>`` in ``html`` to a Markdown pipe table.

    Input is expected to be a table fragment — this is what Cohere Parse puts in
    a table block's ``html`` field. Cell text and ``<caption>`` are carried over;
    any prose *outside* a ``<table>`` is not, so do not call this on a whole
    document body.

    Returns the original HTML unchanged whenever the table cannot be represented
    faithfully — no rows parsed, a nested table, or a ``rowspan`` — so a table is
    either converted correctly or handed on intact for the LLM to read. It is
    never converted into rows whose values sit under the wrong headers.
    """
    if not html or not html.strip():
        return ""

    parser = _HTMLTableParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:  # malformed HTML — keep the original
        logger.warning(f"HTML table parse failed, keeping raw HTML: {e}")
        return html

    rendered = []
    for table in parser.tables:
        if table["unrepresentable"]:
            logger.info(
                "HTML table uses rowspan or nesting, which Markdown cannot "
                "represent faithfully; kept the raw HTML"
            )
            return html
        markdown = _rows_to_markdown(table["rows"], table["row_is_header"])
        if not markdown:
            continue
        if table["caption"]:
            markdown = f"{table['caption']}\n\n{markdown}"
        rendered.append(markdown)

    if not rendered:
        return html
    return "\n\n".join(rendered)


# ---------------------------------------------------------------------------
# Cohere Parse response -> Amazon Textract response format
# ---------------------------------------------------------------------------
#
# Textract geometry uses a normalized 0-1 BoundingBox {Left, Top, Width,
# Height}. Cohere Parse gives `bounding_box_normalized` (0-1
# top_left_x/top_left_y/bottom_right_x/bottom_right_y) on table and image
# elements, so no page dimensions are needed. Its pixel `bounding_box` is
# deliberately ignored: normalizing it would require the source image
# dimensions, which the response does not carry.
#
# Parse returns NO confidence scores, so no block gets a `Confidence` field.
# The OCR service treats confidence and geometry as independently optional, and
# reports missing confidence as "N/A" in the assessment table rather than 0.0.
#
# All lines derived from one table or figure share that element's box. The OCR
# service detects a box reused across LINEs and flags those lines
# `geometrySource: "paragraph"`, which is exactly the right semantics here.


def _bbox_to_geometry(element: dict) -> dict | None:
    """Convert a Cohere normalized bbox to a Textract Geometry dict."""
    bbox = element.get("bounding_box_normalized")
    if not isinstance(bbox, dict):
        return None

    try:
        left = float(bbox["top_left_x"])
        top = float(bbox["top_left_y"])
        right = float(bbox["bottom_right_x"])
        bottom = float(bbox["bottom_right_y"])
    except (KeyError, TypeError, ValueError):
        return None

    left = max(0.0, min(1.0, left))
    top = max(0.0, min(1.0, top))
    width = max(0.0, min(1.0, right - left))
    height = max(0.0, min(1.0, bottom - top))
    if width <= 0.0 or height <= 0.0:
        return None

    return {
        "BoundingBox": {
            "Width": width,
            "Height": height,
            "Left": left,
            "Top": top,
        },
        "Polygon": [
            {"X": left, "Y": top},
            {"X": left + width, "Y": top},
            {"X": left + width, "Y": top + height},
            {"X": left, "Y": top + height},
        ],
    }


def _payload(block: dict, key: str) -> dict:
    """
    Return a block's type-specific payload.

    Cohere's schema nests it under the type name (``{"type": "text", "text":
    {"content": ...}}``), but the docs are loose about this for image and table
    blocks, so fall back to the block itself when the nested object is absent.
    """
    nested = block.get(key)
    return nested if isinstance(nested, dict) else block


def _image_markdown(image: dict) -> str:
    """
    Render an image/figure block the way Parse's markdown mode does.

    The description is collapsed onto one line: Parse writes a multi-sentence
    description of the figure, and a newline inside it would split the
    ``![...](...)`` syntax across separate LINE blocks, leaving fragments of a
    figure caption looking like page text.
    """
    description = " ".join(str(image.get("description") or "").split())
    image_id = str(image.get("id") or "").strip()
    category = str(image.get("category") or "").strip()
    if category and category != "other" and description:
        description = f"{category}: {description}"
    return f"![{description}]({image_id})"


def _rendered_blocks(page: dict) -> list[tuple[str, dict | None]]:
    """
    Flatten one Parse page into ``(text, geometry)`` pairs in reading order.

    Handles both ``output_format`` values: "blocks" (per-element blocks, with
    geometry on tables and images) and "markdown" (one content string, with
    geometry only on images — which are embedded in the content, so nothing to
    attach a box to).
    """
    page_type = page.get("type")

    if page_type == "markdown" or "markdown" in page:
        markdown = _payload(page, "markdown")
        content = str(markdown.get("content") or "")
        return [(content, None)] if content.strip() else []

    rendered: list[tuple[str, dict | None]] = []
    for block in page.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "table":
            table = _payload(block, "table")
            html = str(table.get("html") or "")
            text = html_table_to_markdown(html) if CONVERT_HTML_TABLES else html
            # A short `title` labels the table, so it is kept. The `description`
            # field is deliberately NOT included: it is model-written prose that
            # restates the table's figures, and it restates them wrongly — on a
            # live bank statement it reported the account number as
            # 0035258015143 where the table itself said 003525801543. Feeding
            # that into the OCR text would put fabricated values in front of
            # extraction as if they had been read off the page.
            title = str(table.get("title") or "").strip()
            if title:
                text = f"**{title}**\n\n{text}" if text else f"**{title}**"
            if text.strip():
                rendered.append((text, _bbox_to_geometry(table)))
        elif block_type == "image":
            image = _payload(block, "image")
            rendered.append((_image_markdown(image), _bbox_to_geometry(image)))
        else:
            # "text" and any future block type that carries a content string.
            payload = _payload(block, block_type or "text")
            content = str(payload.get("content") or payload.get("text") or "")
            if content.strip():
                rendered.append((content, _bbox_to_geometry(payload)))

    return rendered


def cohere_page_to_textract(
    page: dict, id_prefix: str = "cohere"
) -> tuple[str, list[dict]]:
    """
    Convert a single Cohere Parse page object into (markdown, textract_blocks).

    Args:
        page: One entry from a Parse response's ``pages`` list.
        id_prefix: Namespace for the generated Block ``Id``s. Every Parse
            response covers a single image and numbers its page ``index`` from
            0, so a multi-image invocation would otherwise mint the same ids
            twice — and the OCR service indexes blocks by ``Id`` to resolve
            LINE->WORD relationships, where a collision silently drops blocks.

    Returns:
        Tuple of (page markdown text, list of Textract-format Block dicts).
    """
    rendered = _rendered_blocks(page)

    blocks: list[dict] = []
    block_id = 0

    def next_id() -> str:
        nonlocal block_id
        block_id += 1
        return f"{id_prefix}-{page.get('index', 0)}-{block_id}"

    # PAGE block. Parse gives no page dimensions, but geometry is already
    # normalized, so a full-page box is always correct.
    blocks.append(
        {
            "BlockType": "PAGE",
            "Id": next_id(),
            "Geometry": {
                "BoundingBox": {"Width": 1.0, "Height": 1.0, "Left": 0.0, "Top": 0.0},
                "Polygon": [
                    {"X": 0.0, "Y": 0.0},
                    {"X": 1.0, "Y": 0.0},
                    {"X": 1.0, "Y": 1.0},
                    {"X": 0.0, "Y": 1.0},
                ],
            },
        }
    )

    # LINE blocks: one per physical line of the rendered content, carrying the
    # source element's geometry when it has one. No Confidence — Cohere Parse
    # does not return confidence scores.
    for text, geometry in rendered:
        for raw_line in text.split("\n"):
            line_text = raw_line.strip()
            if not line_text:
                continue
            line = {"BlockType": "LINE", "Id": next_id(), "Text": line_text}
            if geometry:
                line["Geometry"] = geometry
            blocks.append(line)

    markdown = "\n\n".join(text for text, _ in rendered if text.strip())
    return markdown, blocks


def build_textract_response(
    parse_response: dict, id_prefix: str = "cohere"
) -> tuple[str, dict, int]:
    """
    Build a Textract-format response from a full Cohere Parse API response.

    Args:
        parse_response: A full ``POST /v2/parse`` response body.
        id_prefix: Namespace for generated Block ``Id``s — pass a distinct value
            per image when one invocation parses several (see
            ``cohere_page_to_textract``).

    Returns:
        Tuple of (combined markdown text, textract-format dict with "Blocks"
        and "DocumentMetadata", pages_processed count).
    """
    pages = parse_response.get("pages") or []
    all_text: list[str] = []
    all_blocks: list[dict] = []

    for page in pages:
        if not isinstance(page, dict):
            continue
        markdown, blocks = cohere_page_to_textract(page, id_prefix=id_prefix)
        if markdown:
            all_text.append(markdown)
        all_blocks.extend(blocks)

    meta = parse_response.get("meta") or {}
    billed_units = meta.get("billed_units") or {}
    try:
        pages_processed = int(billed_units.get("pages") or 0)
    except (TypeError, ValueError):
        pages_processed = 0
    pages_processed = pages_processed or len(pages)

    for warning in meta.get("warnings") or []:
        logger.warning(f"Cohere Parse warning: {warning}")

    textract_response = {
        "DocumentMetadata": {"Pages": pages_processed or len(pages)},
        "Blocks": all_blocks,
        # Preserve the Parse model id for traceability
        "ModelId": COHERE_PARSE_MODEL,
    }
    return "\n\n".join(all_text), textract_response, pages_processed


def lambda_handler(event, context):
    """
    Lambda handler that proxies LambdaHook payloads to the Cohere Parse API.

    Expected event format (Converse API-compatible):
    {
        "modelId": "LambdaHook",
        "messages": [{"role": "user", "content": [...]}],
        "system": [{"text": "..."}],
        "inferenceConfig": {"temperature": 0.0, ...},
        "context": "OCR"
    }

    Returns a Converse API-compatible response, augmented with a top-level
    ``textractBlocks`` object (Amazon Textract response format) carrying
    table/figure geometry — but no confidence scores, which Cohere Parse does
    not provide:
    {
        "output": {"message": {"role": "assistant", "content": [{"text": "..."}]}},
        "textractBlocks": {"DocumentMetadata": {...}, "Blocks": [...]},
        "usage": {"pages": N, "inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
    }
    """
    idp_context = event.get("context", "unknown")
    logger.info(f"Received LambdaHook request. Context: {idp_context}")

    messages = event.get("messages", [])
    images = extract_images_from_messages(messages)

    if not images:
        logger.warning("No images found in the payload. Cohere Parse requires images.")
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": ""}]}},
            "usage": {
                "pages": 0,
                "inputTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
            },
        }

    logger.info(
        f"Processing {len(images)} image(s) with Cohere Parse "
        f"(model={COHERE_PARSE_MODEL}, output_format={OUTPUT_FORMAT}, "
        f"convert_html_tables={CONVERT_HTML_TABLES})"
    )

    all_text: list[str] = []
    all_blocks: list[dict] = []
    total_pages = 0

    for i, img in enumerate(images):
        logger.info(f"Parsing image {i + 1}/{len(images)}...")
        parse_response = call_cohere_parse(img["bytes"], img["format"])
        text, textract_response, pages_processed = build_textract_response(
            parse_response, id_prefix=f"cohere-img{i}"
        )
        if text:
            all_text.append(text)
        all_blocks.extend(textract_response["Blocks"])
        total_pages += pages_processed or 1

    combined_text = "\n\n".join(all_text)

    textract_blocks = {
        "DocumentMetadata": {"Pages": total_pages},
        "Blocks": all_blocks,
        "ModelId": COHERE_PARSE_MODEL,
    }

    logger.info(
        f"Cohere Parse complete. Output: {len(combined_text)} chars, "
        f"{len(all_blocks)} blocks, {total_pages} page(s)."
    )

    # Return Converse API-compatible response augmented with Textract blocks.
    # "usage.pages" enables per-page cost metering — add a pricing entry keyed
    # on the function name (e.g. "GENAIIDP-cohere-parse-hook") with unit "pages".
    return {
        "output": {
            "message": {
                "role": "assistant",
                "content": [{"text": combined_text}],
            }
        },
        "textractBlocks": textract_blocks,
        "usage": {
            "pages": total_pages,
            "inputTokens": 0,
            "outputTokens": 0,
            "totalTokens": 0,
        },
    }
