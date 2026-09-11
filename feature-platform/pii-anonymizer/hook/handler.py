# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""preprocessing pipeline hook — PII anonymization.

The host's pipeline-hooks dispatcher invokes this Lambda at the `preprocessing`
extension point — FIRST in the workflow, before the BDA/pipeline routing — with:

    {
      "hookPoint": "preprocessing",
      "featureId": "pii-anonymizer",
      "document": { ... },          # usually a compressed reference (see below)
      "executionArn": "arn:...:execution:..."
    }

What it does, per document:
  1. Resolve the document (compressed reference -> Working bucket JSON).
  2. RE-ENTRANCY GUARD: if the document's name already carries the REDACTED
     marker (stem ends with e.g. "(REDACTED)"), do nothing (halt=false). This is
     the hard stop against an infinite redaction loop — the redacted copy we
     write lands back in the Input bucket and re-triggers processing, and MUST
     NOT be redacted again. Belt-and-suspenders on top of the companion config
     version having no preprocessing hook.
  3. Read this hook's settings from the dispatcher-flattened `argsMap` (generic
     key/value args on the `preprocessing` section): mode, companion_config_version,
     model_id/model_provider, redaction_mode, detection_dpi, store_mapping. This
     keeps the preprocessing step hook-agnostic — no PII-specific config fields.
  4. Run the vendored pii-anonymizer detector+redactor over the source document
     (PDFs go through the image path -> redacted PDF; text/office paths keep
     their native type) writing a redacted copy to a Working-bucket scratch key.
  5. Copy the redacted copy into the Input bucket beside the original with the
     REDACTED marker in its name (e.g. report(REDACTED).pdf), stamping S3
     metadata `config-version=<companion version>` so the spawned execution
     processes it normally (no preprocessing hook).
  5b. OPTIONAL (store_mapping=true): persist the original->synthetic mapping,
     CMK-encrypted, in the FEATURE-OWNED mapping DynamoDB table (never a
     host-proxyable bucket) for the RBAC-gated Redaction Report view.
  6. Halt decision + original handling:
       redactcopy_and_stop     -> halt=true; DELETE the original entirely
                                  (S3 + tracking) so only the redacted copy remains.
       redactcopy_and_continue -> halt=false; the original is also processed.

Idempotency: the redacted key is derived deterministically from the source key,
so a Step Functions retry overwrites the same object instead of spawning
duplicates.

Error posture: onError is set at registration time. For redactcopy_and_stop the
feature registers the hook with onError=fail (better to stop than to leak PII
downstream); for redactcopy_and_continue, onError=continue is acceptable since
the original is expected to carry PII anyway. The handler still returns a
structured error dict on failure so the dispatcher can apply that policy.
"""

from __future__ import annotations

import io
import json
import logging
import os
import sys
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import boto3

# Vendored pii-anonymizer document closure. Its submodules import each other
# with absolute names (`from core...`, `from helpers...`), so the vendored
# package directory must be on sys.path as the import root. See
# vendor/PROVENANCE.md.
_VENDOR_ROOT = os.path.join(os.path.dirname(__file__), "vendor", "pii_anonymizer")
if _VENDOR_ROOT not in sys.path:
    sys.path.insert(0, _VENDOR_ROOT)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

_INPUT_BUCKET = os.environ.get("INPUT_BUCKET", "")
_WORKING_BUCKET = os.environ.get("WORKING_BUCKET", "")
_AUDIT_TABLE = os.environ.get("AUDIT_TABLE_NAME", "")
# Feature-owned table for the original->synthetic PII mapping (a
# re-identification key). Kept OUT of any host-proxyable bucket so it can only be
# read via the feature API's RBAC-gated route.
_MAPPING_TABLE = os.environ.get("MAPPING_TABLE_NAME", "")
# Marker suffix appended to the redacted copy's document id / key stem, e.g.
# `report.pdf` -> `report(REDACTED).pdf`. The re-entrancy guard refuses to run
# on any key whose stem already ends with this marker (prevents an infinite
# redaction loop, since the copy re-enters the Input bucket).
_REDACTED_SUFFIX = os.environ.get("REDACTED_SUFFIX", "(REDACTED)")
# Fallback companion version name if the hook args omit one.
_DEFAULT_COMPANION_VERSION = os.environ.get(
    "DEFAULT_COMPANION_CONFIG_VERSION", "default"
)

# Redaction modes (the `mode` hook arg). Names mirror the wizard-generated
# config-version suffixes so operators can reason about them consistently:
#   redactcopy_and_stop     -> write redacted copy, DELETE the original
#   redactcopy_and_continue -> write redacted copy, also process the original
_MODE_STOP = "redactcopy_and_stop"
_MODE_CONTINUE = "redactcopy_and_continue"
_VALID_MODES = {_MODE_STOP, _MODE_CONTINUE}

_s3 = boto3.client("s3")
_dynamodb = boto3.resource("dynamodb")
# Bedrock client the vendored processors expect to be handed in (Converse API).
_bedrock = boto3.client("bedrock-runtime")


# ---------------------------------------------------------------------------
# Small inline helpers
# ---------------------------------------------------------------------------
def _read_s3_json(uri: str) -> Optional[Dict[str, Any]]:
    parsed = urlparse(uri)
    try:
        resp = _s3.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))
        body = json.loads(resp["Body"].read().decode("utf-8"))
        return body if isinstance(body, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read %s: %s", uri, exc)
        return None


def _load_document(raw: Any) -> Optional[Dict[str, Any]]:
    """Resolve the hook payload's document to a plain dict.

    A compressed reference is `{"compressed": true, "s3_uri": ...}` pointing at
    the full Document JSON in the host's Working bucket.
    """
    if not isinstance(raw, dict):
        return None
    if raw.get("compressed") is True:
        uri = raw.get("s3_uri")
        if not uri:
            logger.warning("Compressed document reference without s3_uri")
            return None
        return _read_s3_json(uri)
    return raw


def _skip(document_id: Optional[str], reason: str) -> Dict[str, Any]:
    logger.info("PII preprocessing skipped for %s: %s", document_id, reason)
    return {"halt": False, "skipped": True, "documentId": document_id, "reason": reason}


# ---------------------------------------------------------------------------
# File-type routing -> vendored processor
# ---------------------------------------------------------------------------
_TEXT_EXTS = {"txt", "csv", "json"}
_IMAGE_EXTS = {"jpg", "jpeg", "png", "tiff", "tif", "bmp", "webp"}


def _ext_of(key: str) -> str:
    return key.rsplit(".", 1)[-1].lower() if "." in key else ""


def _xls_cell_value(cell: Any, datemode: int) -> Any:
    """One BIFF cell as a value openpyxl can write.

    Formula cells arrive as their cached NUMBER/TEXT result, which is what a
    reader of the sheet would see and therefore what PII detection should scan.
    """
    import xlrd

    if cell.ctype == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate.xldate_as_datetime(cell.value, datemode)
        except (ValueError, OverflowError):
            # An out-of-range serial is not worth failing a redaction over; the
            # raw number still carries whatever the cell showed.
            return cell.value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK, xlrd.XL_CELL_ERROR):
        return None
    if cell.ctype == xlrd.XL_CELL_NUMBER and float(cell.value).is_integer():
        # Excel stores every number as a float; writing 42.0 where the sheet
        # showed 42 changes what the detector sees and what the reviewer reads.
        return int(cell.value)
    return cell.value


def _convert_xls_to_xlsx(source_bucket: str, source_key: str, dest_key: str) -> str:
    """Rewrite a legacy BIFF ``.xls`` as ``.xlsx`` in the Working bucket.

    The vendored redactor reads workbooks with ``openpyxl``, which handles only
    the OOXML container. On a ``.xls`` it fails with ``zipfile.BadZipFile`` — not
    the ``InvalidFileException`` you would expect, because that check is on the
    filename and the processor downloads to a temp path it names ``*.xlsx``
    regardless — and because this extension is *claimed* as supported it never
    reaches the handler's unsupported-format path. Converting here keeps the fix in accelerator-owned
    code: the vendored subtree stays byte-identical to its upstream provenance,
    and the redacted output was already ``.xlsx`` for both extensions, so the
    contract downstream is unchanged.

    ``xlrd`` (BIFF-only since 2.0) is the same engine pandas uses for this
    format, and it is pure Python, so it adds no wheel-architecture constraint to
    this function's build.

    Returns the Working-bucket key of the converted workbook.
    """
    import xlrd
    from openpyxl import Workbook

    raw = _s3.get_object(Bucket=source_bucket, Key=source_key)["Body"].read()
    book = xlrd.open_workbook(file_contents=raw)

    workbook = Workbook()
    workbook.remove(workbook.active)  # drop the default sheet Workbook() creates
    for sheet in book.sheets():
        # Excel caps a sheet name at 31 chars; a BIFF name is already within it,
        # but truncating defensively beats an openpyxl error mid-redaction.
        worksheet = workbook.create_sheet(title=sheet.name[:31])
        for row in range(sheet.nrows):
            for col in range(sheet.ncols):
                value = _xls_cell_value(sheet.cell(row, col), book.datemode)
                if value is not None:
                    worksheet.cell(row=row + 1, column=col + 1, value=value)

    buffer = io.BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    _s3.put_object(Bucket=_WORKING_BUCKET, Key=dest_key, Body=buffer.getvalue())
    logger.info(
        "Converted legacy .xls to .xlsx for redaction: s3://%s/%s -> s3://%s/%s "
        "(%d sheet(s))",
        source_bucket,
        source_key,
        _WORKING_BUCKET,
        dest_key,
        len(book.sheets()),
    )
    return dest_key


def _build_pii_config(args: Dict[str, str]) -> Dict[str, Any]:
    """Build the plain-dict config the vendored processors expect from the
    hook's generic key/value args (the `argsMap` the dispatcher passes). All
    values are strings. Sensible defaults keep it working with no args at all.

    Recognized args:
      model_id, model_provider, redaction_mode ("synthetic"|"blackout"),
      detection_dpi.
    """
    # Default to Claude Haiku: PII detection over dense forms (W2, lending) needs
    # a large output-token budget — Nova Lite's smaller cap truncates the
    # detection JSON, and the vendored detector fails loudly (by design, to never
    # silently drop PII). Haiku balances recall/cost; Nova Lite stays selectable.
    model_id = args.get("model_id") or "us.anthropic.claude-haiku-4-5-20251001-v1:0"
    provider = args.get("model_provider") or (
        "amazon" if ("nova" in model_id or "titan" in model_id) else "anthropic"
    )
    redaction_mode = args.get("redaction_mode") or "synthetic"
    try:
        dpi = int(args.get("detection_dpi") or 300)
    except (TypeError, ValueError):
        dpi = 300
    return {
        "model": {"id": model_id, "provider": provider},
        "redaction": {"mode": redaction_mode},
        # Blocks the vendored processors access with a hard `config[...]` (NOT
        # .get) — omitting them KeyErrors the image path:
        #   pdf_image_processor: config["performance"]["dpi"],
        #                        config["processing"]["process_embedded_images"]
        "performance": {"dpi": dpi, "max_retries": 3, "timeout_seconds": 300},
        "processing": {"approach": "image", "process_embedded_images": False},
    }


def _redact_to_scratch(
    document: Dict[str, Any], pii_config: Dict[str, Any], doc_id: str
) -> Optional[Dict[str, Any]]:
    """Run the vendored detector+redactor. Writes the redacted copy to a
    Working-bucket scratch key and returns {scratch_key, out_ext, pii_count},
    or None if the format is unsupported. Raises on redaction failure."""
    input_bucket = document.get("input_bucket") or _INPUT_BUCKET
    input_key = document["input_key"]
    ext = _ext_of(input_key)
    base = os.path.basename(input_key).rsplit(".", 1)[0] if "." in input_key else doc_id
    scratch_folder = f"pii_scratch/{doc_id}/"

    if ext == "pdf":
        # ALWAYS use the image path for PDFs so the redacted copy is a real
        # PDF (pages rasterized, PII white-boxed/synthesized, flattened to image
        # — no leaked text layer). The text path (process_pdf_text_based) only
        # emits a .txt of the extracted text, discarding layout, which is not a
        # usable "redacted PDF". The image path OCRs even text-native PDFs via
        # Textract, so it covers both scanned and digital PDFs. Trade-off:
        # Textract+vision cost on every PDF — acceptable for PDF-in/PDF-out.
        from processors.pdf_image_processor import process_pdf_image_based

        logger.info("PDF path: image (PDF-in/PDF-out)")
        result = process_pdf_image_based(
            input_bucket,
            input_key,
            _WORKING_BUCKET,
            base,
            pii_config,
            _bedrock,
            None,
            _s3,
            scratch_folder,
        )
        out_ext = "pdf"
    elif ext in ("txt", "csv"):
        # CSV is line-based text — the txt processor chunks by lines and
        # redacts correctly. process_excel_file is .xlsx-specific (openpyxl),
        # so it is NOT used for CSV. The redacted CSV is written back as text
        # (still ingested fine by the host OCR), which is acceptable for v1.
        from processors.txt_processor import process_txt_file

        result = process_txt_file(
            input_bucket,
            input_key,
            _WORKING_BUCKET,
            base,
            pii_config,
            _bedrock,
            None,
            _s3,
            scratch_folder,
        )
        out_ext = ext
    elif ext in ("xlsx", "xls"):
        from processors.tabular_processor import process_excel_file

        source_bucket, source_key = input_bucket, input_key
        if ext == "xls":
            # openpyxl (the vendored processor's engine) cannot read BIFF at all,
            # so convert first. Reading the ORIGINAL from the Input bucket and
            # writing the conversion to the Working bucket keeps the original
            # untouched — the halt/delete decision downstream still applies to it.
            source_bucket = _WORKING_BUCKET
            source_key = _convert_xls_to_xlsx(
                input_bucket, input_key, f"{scratch_folder}{base}.converted.xlsx"
            )

        result = process_excel_file(
            source_bucket,
            source_key,
            _WORKING_BUCKET,
            base,
            pii_config,
            _bedrock,
            None,
            _s3,
            scratch_folder,
        )
        out_ext = "xlsx"
    elif ext in ("docx", "doc"):
        # A legacy binary .doc cannot be read here either: the vendored word
        # processor uses python-docx, which reads the OOXML container only, and no
        # pure-Python reader exists for the OLE2 format (host-side: #829). It
        # already failed — `PackageNotFoundError` from deep inside the processor —
        # so this changes the message, not the outcome. The outcome is the right
        # one for a redaction feature: the shipped preset sets `onError: fail`, so
        # the run stops rather than passing an unredacted document through, which
        # is what routing it to the unsupported-format path below would do.
        if ext == "doc":
            raise RuntimeError(
                f"Cannot redact {input_key}: legacy binary Word (.doc) is not "
                "readable — only .docx is. Re-save the document as .docx and "
                "upload it again. (Failing rather than passing it through: an "
                "unredacted document must not continue.)"
            )
        from processors.word_processor import process_word_file

        result = process_word_file(
            input_bucket,
            input_key,
            _WORKING_BUCKET,
            base,
            pii_config,
            _bedrock,
            None,
            _s3,
            scratch_folder,
        )
        out_ext = "docx"
    elif ext in _IMAGE_EXTS:
        from processors.image_processor import process_image_file

        result = process_image_file(
            input_bucket,
            input_key,
            _WORKING_BUCKET,
            base,
            pii_config,
            _bedrock,
            None,
            _s3,
            scratch_folder,
        )
        out_ext = ext
    else:
        logger.warning(
            "Unsupported format for PII redaction: .%s (key=%s)", ext, input_key
        )
        return None

    if not isinstance(result, dict) or not result.get("success"):
        err = result.get("error") if isinstance(result, dict) else result
        raise RuntimeError(f"Redaction failed for {input_key}: {err}")
    scratch_key = result.get("s3_output_file")
    if not scratch_key:
        raise RuntimeError(f"Redaction produced no output key for {input_key}")
    # The redacted copy's extension MUST match what the processor actually wrote,
    # not the input's extension. The text-PDF path (process_pdf_text_based)
    # redacts extracted TEXT and writes `redacted_<name>.txt` — copying that body
    # to a `.pdf` key produces an invalid PDF that fails downstream OCR. Trust the
    # scratch key's real extension so the redacted copy is ingested as the right
    # type (a text-native PDF becomes a redacted .txt — the host handles it).
    actual_ext = _ext_of(scratch_key) or out_ext
    return {
        "scratch_key": scratch_key,
        "out_ext": actual_ext,
        "pii_count": result.get("pii_count", 0),
        "replacements": result.get("replacements"),
        # original -> synthetic value map (synthetic mode). Contains real PII;
        # only persisted when store_mapping is enabled. Absent for blackout.
        "mapping": result.get("pii_mapping"),
    }


def _stem_ext(key: str) -> tuple[str, str]:
    """Split a key into (stem, ext) where ext excludes the dot; ext='' if none."""
    if "." in os.path.basename(key):
        stem, ext = key.rsplit(".", 1)
        return stem, ext
    return key, ""


def _is_redacted_key(input_key: str) -> bool:
    """Belt-and-suspenders re-entrancy guard: True if this key looks like a
    redacted copy (stem ends with the marker).

    The PRIMARY guard against infinite redaction is that the redacted copy is
    stamped with the COMPANION config version, which has NO preprocessing hook —
    so this hook never runs on a copy. This filename check is a secondary
    safety net. It is fail-SAFE: a user who literally names a source
    `report(REDACTED).pdf` is passed through un-redacted (redaction skipped),
    which never leaks PII — it just doesn't redact that oddly-named file."""
    stem, _ = _stem_ext(input_key.lstrip("/"))
    return stem.endswith(_REDACTED_SUFFIX)


def _redacted_input_key(input_key: str, out_ext: Optional[str] = None) -> str:
    """Deterministic key for the redacted copy (idempotent): append the marker
    suffix to the stem, keeping it beside the original in the Input bucket.
    e.g. `sub/report.pdf` -> `sub/report(REDACTED).pdf`.

    When the redaction output format differs from the input (e.g. a text-native
    PDF is redacted to .txt), the extension is rewritten to out_ext so the host
    ingests the redacted copy as the correct type."""
    stem, ext = _stem_ext(input_key.lstrip("/"))
    ext = (out_ext or ext or "").lstrip(".")
    stem = f"{stem}{_REDACTED_SUFFIX}"
    return f"{stem}.{ext}" if ext else stem


def _now_iso() -> str:
    # Lambda-safe UTC timestamp for the audit row (no external clock dep).
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _write_audit(row: Dict[str, Any]) -> None:
    """Best-effort audit row (metadata only — NEVER any PII). Failures are
    logged, never fatal: the audit trail must not break the pipeline."""
    if not _AUDIT_TABLE:
        return
    try:
        _dynamodb.Table(_AUDIT_TABLE).put_item(Item=row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Audit write failed (ignored): %s", exc)


def _store_mapping(
    document_id: str, original_config_version: str, mapping: Dict[str, Any]
) -> bool:
    """Persist the original->synthetic mapping in the FEATURE-OWNED mapping
    DynamoDB table (CMK-encrypted). Returns True on success.

    SECURITY: the mapping is a re-identification key (contains REAL PII). It is
    stored in a feature-owned table — NOT the host Output/Working buckets —
    precisely because the host's getFileContents resolver will proxy any
    Output-bucket key to any authenticated user (no config-version scoping),
    which would bypass the RBAC gate. Kept out of any host-proxyable location,
    the mapping is reachable ONLY via the feature API's RBAC-gated
    /report/{docId}/mapping route (feature-api/handler.py), which checks the
    caller's allowedConfigVersions against original_config_version."""
    if not _MAPPING_TABLE:
        logger.warning("MAPPING_TABLE_NAME not set; cannot store mapping")
        return False
    try:
        _dynamodb.Table(_MAPPING_TABLE).put_item(
            Item={
                "documentId": document_id,
                "originalConfigVersion": original_config_version,
                "createdAt": _now_iso(),
                # {original PII value: synthetic replacement}
                "mapping": mapping,
            }
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("Mapping store failed (ignored): %s", exc)
        return False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------
def lambda_handler(event: Dict[str, Any], _context: Any) -> Dict[str, Any]:
    logger.info(
        "pii-anonymizer preprocessing hook invoked: hookPoint=%s executionArn=%s",
        event.get("hookPoint"),
        event.get("executionArn"),
    )

    document = _load_document(event.get("document"))
    if not document:
        return _skip(None, "document payload missing or unresolvable")
    document_id = document.get("id") or document.get("input_key")
    input_key = document.get("input_key")
    if not input_key:
        return _skip(document_id, "document has no input_key")

    # (2) RE-ENTRANCY GUARD — never redact a redacted copy. Fail closed.
    if _is_redacted_key(input_key):
        return _skip(document_id, "input is already a redacted copy (REDACTED marker)")

    # (3) settings from the hook's generic args (the dispatcher-flattened
    # argsMap). This keeps the pipeline hook-agnostic — all PII specifics live
    # in the hook's args, not in named config fields.
    args = event.get("argsMap") or {}
    version = document.get("config_version") or _DEFAULT_COMPANION_VERSION
    mode = args.get("mode") or _MODE_STOP
    if mode not in _VALID_MODES:
        logger.warning("Unknown mode %r; defaulting to %s", mode, _MODE_STOP)
        mode = _MODE_STOP
    companion_version = (
        args.get("companion_config_version") or _DEFAULT_COMPANION_VERSION
    )
    store_mapping = str(args.get("store_mapping", "false")).lower() == "true"
    pii_config = _build_pii_config(args)

    if not _INPUT_BUCKET or not _WORKING_BUCKET:
        return _skip(document_id, "INPUT_BUCKET/WORKING_BUCKET env not set")

    # (4) redact to a Working-bucket scratch key
    redaction = _redact_to_scratch(document, pii_config, document_id or "doc")
    if redaction is None:
        # Unsupported format. Do NOT halt — let the original process normally
        # rather than silently dropping the document.
        return _skip(document_id, "unsupported format; passed through unredacted")

    # (5) copy the redacted copy into the Input bucket beside the original with
    # the REDACTED marker, stamping the companion config-version as S3 metadata
    # so the spawned execution processes it normally (no preprocessing hook).
    redacted_key = _redacted_input_key(input_key, redaction.get("out_ext"))
    _s3.copy_object(
        CopySource={"Bucket": _WORKING_BUCKET, "Key": redaction["scratch_key"]},
        Bucket=_INPUT_BUCKET,
        Key=redacted_key,
        MetadataDirective="REPLACE",
        Metadata={"config-version": companion_version},
    )
    logger.info(
        "Wrote redacted copy s3://%s/%s (config-version=%s, pii_count=%s, mode=%s)",
        _INPUT_BUCKET,
        redacted_key,
        companion_version,
        redaction["pii_count"],
        mode,
    )

    # (5b) OPTIONAL: persist the original->synthetic mapping (a re-identification
    # key — contains real PII). Stored CMK-encrypted in the feature-owned
    # mapping DynamoDB table (see _store_mapping for why never a host bucket);
    # the Redaction Report only reveals it to users with access to the
    # ORIGINAL's config version. Off unless store_mapping=true.
    mapping_stored = False
    if store_mapping and redaction.get("mapping"):
        mapping_stored = _store_mapping(
            document_id or input_key, version, redaction["mapping"]
        )

    halt = mode == _MODE_STOP

    # (6) audit row — metadata only, never PII. Records the ORIGINAL's config
    # version so the report can RBAC-gate the mapping view. NOTE: we record only
    # a `mappingStored` boolean — never a location/URI. The mapping lives in a
    # feature-owned DynamoDB table (not any host-proxyable bucket) and is fetched
    # only through the RBAC-gated feature-API route keyed by documentId.
    _write_audit(
        {
            "documentId": document_id,
            "gsiPk": "ALL",
            "createdAt": _now_iso(),
            "sourceKey": input_key,
            "redactedKey": redacted_key,
            "mode": mode,
            "companionConfigVersion": companion_version,
            "originalConfigVersion": version,
            "piiCount": int(redaction["pii_count"] or 0),
            "replacements": int(redaction.get("replacements") or 0),
            "halted": halt,
            "mappingStored": mapping_stored,
            "executionArn": event.get("executionArn") or "",
        }
    )

    # (7) In redactcopy_and_stop mode the hook returns halt=true; the workflow's
    # terminal state sets REDACTED_SUPERSEDED and the HOST workflow_tracker then
    # deletes the original entirely (S3 + tracking). The delete is done there —
    # NOT here — because the tracker is the last writer for the execution;
    # deleting mid-execution would race with the tracker re-creating the row.
    return {
        "halt": halt,
        "documentId": document_id,
        "mode": mode,
        "redactedKey": redacted_key,
        "companionConfigVersion": companion_version,
        "piiCount": redaction["pii_count"],
        "replacements": redaction.get("replacements"),
        "mappingStored": mapping_stored,
    }
