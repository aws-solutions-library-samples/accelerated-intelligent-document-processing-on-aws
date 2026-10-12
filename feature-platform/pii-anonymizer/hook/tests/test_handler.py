# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the PII Anonymization preprocessing hook.

These pin the safety-critical behaviors WITHOUT invoking Bedrock/Textract or
the vendored redactor (redaction itself is exercised in integration/E2E):

- re-entrancy guard: a redacted-prefix input is skipped (no loop)
- halt decision derives from mode (redacted_only -> halt, else continue)
- redacted copy is written to the Input bucket under the reserved prefix
  with the companion config-version stamped as S3 metadata
- unsupported formats pass through unredacted (no halt, no crash)
- config precedence: mode/companion come from the document's config version
"""

import importlib
import os
import sys

import pytest

HOOK_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("INPUT_BUCKET", "input-bkt")
    monkeypatch.setenv("WORKING_BUCKET", "working-bkt")
    monkeypatch.setenv("CONFIGURATION_TABLE_NAME", "ConfigTable")
    monkeypatch.setenv("REDACTED_SUFFIX", "(REDACTED)")
    sys.path.insert(0, HOOK_DIR)
    yield
    sys.path.remove(HOOK_DIR)
    sys.modules.pop("handler", None)


def _load():
    if "handler" in sys.modules:
        del sys.modules["handler"]
    return importlib.import_module("handler")


def test_reentrancy_guard_skips_redacted_input():
    mod = _load()
    # A document whose name already carries the REDACTED marker must be skipped
    # with halt=false and MUST NOT trigger any redaction.
    called = {"redact": False}
    mod._redact_to_scratch = lambda *a, **k: called.__setitem__("redact", True)  # type: ignore
    out = mod.lambda_handler(
        {
            "hookPoint": "preprocessing",
            "document": {"input_key": "foo(REDACTED).pdf", "id": "x"},
        },
        None,
    )
    assert out["halt"] is False
    assert out["skipped"] is True
    assert "already a redacted copy" in out["reason"]
    assert called["redact"] is False


def test_missing_input_key_skips():
    mod = _load()
    out = mod.lambda_handler(
        {"hookPoint": "preprocessing", "document": {"id": "x"}}, None
    )
    assert out["halt"] is False and out["skipped"] is True


def _redaction_stub(**over):
    base = {
        "scratch_key": "pii_scratch/x/redacted_foo.txt",
        "out_ext": "pdf",
        "pii_count": 3,
        "replacements": 3,
        "mapping": None,
    }
    base.update(over)
    return base


def test_stop_mode_halts_and_writes_copy(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        mod, "_redact_to_scratch", lambda doc, cfg, did: _redaction_stub()
    )
    copies = {}
    monkeypatch.setattr(mod._s3, "copy_object", lambda **kw: copies.update(kw) or {})
    out = mod.lambda_handler(
        {
            "hookPoint": "preprocessing",
            "argsMap": {
                "mode": "redactcopy_and_stop",
                "companion_config_version": "base__pii_target",
            },
            "document": {
                "input_key": "foo.pdf",
                "id": "foo.pdf",
                "input_bucket": "input-bkt",
                "config_version": "base__pii_stop",
            },
        },
        None,
    )
    # halt=true signals the workflow's terminal path; the HOST tracker deletes
    # the original (race-free) — the hook does NOT delete.
    assert out["halt"] is True
    assert out["redactedKey"] == "foo(REDACTED).pdf"
    assert out["companionConfigVersion"] == "base__pii_target"
    assert copies["Bucket"] == "input-bkt"
    assert copies["Key"] == "foo(REDACTED).pdf"
    # `submission-source` is what exempts this copy from config prefix mappings
    # (idp_common.config.prefix_mappings). It is asserted here, in the same place as
    # the companion version, because the copy lands BESIDE the original: without it,
    # a mapping on the original's prefix governs the copy too, and a mapping in
    # `reject` mode refuses it — after which this mode's halt=true has already told
    # the host to delete the original. That combination loses the document outright,
    # so the exemption is a correctness property of this write and not a nicety.
    assert copies["Metadata"] == {
        "config-version": "base__pii_target",
        "submission-source": "pii-anonymizer",
    }


def test_continue_mode_does_not_halt(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        mod, "_redact_to_scratch", lambda doc, cfg, did: _redaction_stub()
    )
    monkeypatch.setattr(mod._s3, "copy_object", lambda **kw: {})
    out = mod.lambda_handler(
        {
            "hookPoint": "preprocessing",
            "argsMap": {
                "mode": "redactcopy_and_continue",
                "companion_config_version": "base__pii_target",
            },
            "document": {
                "input_key": "a.pdf",
                "id": "a.pdf",
                "config_version": "base__pii_go",
            },
        },
        None,
    )
    assert out["halt"] is False
    assert out["redactedKey"] == "a(REDACTED).pdf"


def test_store_mapping_opt_in(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        mod,
        "_redact_to_scratch",
        lambda doc, cfg, did: _redaction_stub(mapping={"John Smith": "Jane Doe"}),
    )
    monkeypatch.setattr(mod._s3, "copy_object", lambda **kw: {})
    stored = {}
    monkeypatch.setattr(
        mod,
        "_store_mapping",
        lambda did, ver, m: bool(stored.update({"m": m, "ver": ver}) or True),
    )
    # store_mapping=false -> not stored
    out = mod.lambda_handler(
        {
            "hookPoint": "preprocessing",
            "argsMap": {"mode": "redactcopy_and_continue"},
            "document": {"input_key": "a.pdf", "id": "a.pdf", "config_version": "v1"},
        },
        None,
    )
    assert out["mappingStored"] is False
    assert stored == {}
    # store_mapping=true -> stored, keyed to the ORIGINAL's config version
    out2 = mod.lambda_handler(
        {
            "hookPoint": "preprocessing",
            "argsMap": {"mode": "redactcopy_and_continue", "store_mapping": "true"},
            "document": {"input_key": "b.pdf", "id": "b.pdf", "config_version": "v1"},
        },
        None,
    )
    assert out2["mappingStored"] is True
    assert stored["ver"] == "v1"
    assert stored["m"] == {"John Smith": "Jane Doe"}


def test_unsupported_format_passes_through(monkeypatch):
    mod = _load()
    monkeypatch.setattr(mod, "_redact_to_scratch", lambda doc, cfg, did: None)
    out = mod.lambda_handler(
        {
            "hookPoint": "preprocessing",
            "argsMap": {"mode": "redactcopy_and_stop"},
            "document": {"input_key": "movie.mp3", "id": "m"},
        },
        None,
    )
    assert out["halt"] is False
    assert out["skipped"] is True
    assert "unsupported" in out["reason"]


def test_unknown_mode_defaults_to_stop(monkeypatch):
    mod = _load()
    monkeypatch.setattr(
        mod, "_redact_to_scratch", lambda doc, cfg, did: _redaction_stub()
    )
    monkeypatch.setattr(mod._s3, "copy_object", lambda **kw: {})
    out = mod.lambda_handler(
        {
            "hookPoint": "preprocessing",
            "argsMap": {"mode": "banana"},
            "document": {"input_key": "a.pdf", "id": "a.pdf", "config_version": "v1"},
        },
        None,
    )
    assert out["mode"] == mod._MODE_STOP
    assert out["halt"] is True


def test_build_pii_config_from_args():
    mod = _load()
    cfg = mod._build_pii_config({})
    # Default is Claude Haiku (large output budget for dense forms).
    assert cfg["model"]["provider"] == "anthropic"
    assert "haiku" in cfg["model"]["id"]
    assert cfg["redaction"]["mode"] == "synthetic"
    # Required hard-accessed blocks for the image path are always present.
    assert cfg["performance"]["dpi"] == 300
    assert "process_embedded_images" in cfg["processing"]
    # amazon inferred for a nova id; dpi + redaction from args
    cfg2 = mod._build_pii_config(
        {
            "model_id": "us.amazon.nova-lite-v1:0",
            "redaction_mode": "blackout",
            "detection_dpi": "150",
        }
    )
    assert cfg2["model"]["provider"] == "amazon"
    assert cfg2["redaction"]["mode"] == "blackout"
    assert cfg2["performance"]["dpi"] == 150


def test_redacted_input_key_deterministic():
    mod = _load()
    # Marker appended to the stem, beside the original (same folder).
    assert mod._redacted_input_key("sub/dir/doc.pdf") == "sub/dir/doc(REDACTED).pdf"
    assert mod._redacted_input_key("/leading.pdf") == "leading(REDACTED).pdf"


def test_redacted_input_key_rewrites_extension():
    """When the redaction output format differs, the copy key carries the real
    output extension so the host ingests it as the right type."""
    mod = _load()
    assert mod._redacted_input_key("w2.pdf", "txt") == "w2(REDACTED).txt"
    assert mod._redacted_input_key("a/b/scan.png", "png") == "a/b/scan(REDACTED).png"


def test_is_redacted_key_guard():
    mod = _load()
    assert mod._is_redacted_key("foo(REDACTED).pdf") is True
    assert mod._is_redacted_key("dir/bar(REDACTED).txt") is True
    assert mod._is_redacted_key("foo.pdf") is False
    # marker only counts when it's the stem suffix, not anywhere in the name
    assert mod._is_redacted_key("foo(REDACTED)extra.pdf") is False
