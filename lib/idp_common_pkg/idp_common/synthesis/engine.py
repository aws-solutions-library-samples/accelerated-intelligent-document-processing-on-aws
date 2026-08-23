# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Runtime-agnostic adapter to the SEED document generator (``seed-data``).

This module is the SINGLE seam between the accelerator and the standalone
generator. It is imported lazily (never from ``synthesis/__init__.py``) so that
schema authoring, the catalog and the bridge all work without the generator
installed.

Two responsibilities:

1. :func:`generator_available` - a cheap capability probe. The whole feature
   degrades by *capability*: only document generation needs the generator;
   schema authoring + config/test-set creation always work. Callers (the Quick
   Start Agent, the ``idp-cli bootstrap`` command, a ``bootstrapCapabilities``
   GraphQL field) check this first and fall back gracefully with install
   guidance rather than failing.

2. :func:`synthesize` - generate ``count`` labeled documents from a written
   ``schema_dir`` and report progress via an injected ``status_cb``. Because
   the host injects ``status_cb`` and this function imports the generator
   lazily, the exact same entrypoint runs in a container Lambda, an AgentCore
   Runtime, or locally.

The generator's import path / entrypoint is intentionally indirected through
``_import_generator`` so the packaging decision is isolated to one place. The
generator is the published ``seed-data`` package (module ``seed_data``); we call
its typed ``Generator`` facade (``seed_data.Generator.generate_batch``).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Progress callback: (percent_complete: float 0-100, message: str) -> None
StatusCallback = Callable[[float, str], None]

# Install guidance surfaced when the generator is unavailable. Kept here so the
# CLI, the agent, and the GraphQL capability field all give the same message.
INSTALL_HINT = (
    "The synthetic document generator is not installed. Install it with "
    "`pip install seed-data` (or `idp_common[synthesis-generator]`), or in a "
    "deployed stack set the "
    "`EnableConfigBootstrap` parameter to true (requires an AgentCore Runtime "
    "in a supported region). Schema authoring and config creation still work "
    "without it; you can also upload your own example documents to build a "
    "test set."
)


@dataclass
class SynthesisJob:
    """Inputs for one synthesis run. Runtime-agnostic (no AWS handles)."""

    schema_dir: str
    out_dir: str
    count: int = 3
    threshold: int = 7
    augment: bool = False
    extra: Optional[str] = None
    model_id: Optional[str] = None
    sample_pdfs: List[str] = field(default_factory=list)


@dataclass
class SynthesisUsage:
    """What one run actually consumed.

    Recorded because a cost or duration estimate is only as good as the runs it
    was calibrated on, and none of this is recoverable after the fact: the
    generator reports it per document and nothing else persists it.

    ``attempts`` counts pipeline node executions, which rise with the critic
    threshold — a document rejected and re-rendered costs another pass. That is
    the dominant source of cost variance between runs.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    attempts: int = 0
    docs_measured: int = 0
    scores: List[int] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "inputTokens": self.input_tokens,
            "outputTokens": self.output_tokens,
            "totalTokens": self.total_tokens,
            "attempts": self.attempts,
            "docsMeasured": self.docs_measured,
            "meanScore": (
                round(sum(self.scores) / len(self.scores), 2) if self.scores else None
            ),
        }


@dataclass
class SynthesisResult:
    """Outputs of a synthesis run."""

    success: bool
    packet_dir: Optional[str] = None
    docs_completed: int = 0
    docs_requested: int = 0
    error: Optional[str] = None
    usage: Optional[SynthesisUsage] = None


def _import_generator():
    """Import the SEED generator module, or raise ImportError.

    Indirected so the packaging mechanism is isolated. The published package is
    ``seed-data`` (module ``seed_data``); the legacy ``doc_gen_agent`` name is
    kept as a transitional fallback for older/vendored builds.
    """
    try:
        import seed_data  # type: ignore  # noqa: F401
    except ImportError:
        import doc_gen_agent as seed_data  # type: ignore  # noqa: F401

    # Require the typed Generator facade (seed-data >= 0.0.5). An older build
    # that only exposed run_batch would import fine but fail in synthesize(), so
    # treat a missing Generator as "not available" with actionable guidance.
    if not hasattr(seed_data, "Generator"):
        raise ImportError(
            "Installed generator lacks the 'Generator' API (needs seed-data >= 0.0.5)"
        )
    return seed_data


def generator_available() -> Tuple[bool, str]:
    """Return ``(available, reason)`` for the document generator.

    Cheap, side-effect-free import probe. ``reason`` is empty when available,
    otherwise a human-readable explanation (the import error) suitable for
    surfacing alongside :data:`INSTALL_HINT`.
    """
    try:
        _import_generator()
        return True, ""
    except ImportError as e:
        return False, str(e)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Unexpected error probing generator availability: %s", e)
        return False, str(e)


def _seed_model_key(model_id: str) -> str:
    """Map a raw Bedrock model ID back to its SEED registry key when known.

    SEED's registry keys carry a high per-model max_tokens (e.g. 63999); a raw
    model ID that isn't a key falls back to SEED's 8192 default, which truncates
    large documents (bank statements, long tables). Return the matching key so
    the model keeps its real output budget; pass unknown IDs through unchanged.
    """
    try:
        from seed_data.utils import MODELS

        for key, entry in MODELS.items():
            if entry.get("model_id") == model_id:
                return key
    except Exception:  # pragma: no cover - defensive
        pass
    return model_id


def _seed_node_timeout_s() -> int:
    """Per-stage timeout for the generation pipeline."""
    return int(os.environ.get("SEED_NODE_TIMEOUT_S", str(3 * 3600)))


def _seed_pipeline_timeout_s() -> int:
    """Whole-pipeline timeout, always strictly greater than the per-stage one.

    These two are easy to misorder, and the failure is silent: a pipeline timeout
    at or below the stage timeout means a stage can never use the budget it was
    given, so raising the stage timeout appears to do nothing. Held at twice the
    stage timeout unless overridden higher.
    """
    node_s = _seed_node_timeout_s()
    configured = int(os.environ.get("SEED_PIPELINE_TIMEOUT_S", str(2 * node_s)))
    return max(configured, node_s * 2)


def _raise_seed_node_timeout() -> None:
    """Raise SEED's per-node timeout default (600s) for long high-quality runs.

    seed-data 0.0.6 added a hard per-node cap in ``build_pipeline_graph``
    (``node_timeout=600``) as a wedge safety net, but a 600s cap kills
    legitimately slow nodes (a 20-doc high-quality doc_loop can exceed it). The
    Generator API does not expose the knob, so adjust the function's default —
    ``batch.py`` holds a reference to the same function object, so this covers
    the fan-out path too. Our runtime watchdog (just under the ~8h AgentCore
    session ceiling) remains the terminal backstop. Skipped gracefully if SEED's
    signature changes.

    The default is 3h, not 1h. The cap applies per *stage*, and one stage renders
    a whole batch, so its cost scales with count x quality x augmentation rather
    than with a single document: a 49-document high-quality augmented run spent
    over an hour in doc_loop and was killed with every document lost. Prefer
    raising this over silently truncating a run. Tunable via
    SEED_NODE_TIMEOUT_S, which the generator extension exposes as a stack
    parameter.
    """
    timeout_s = _seed_node_timeout_s()
    try:
        import inspect

        from seed_data.stages.pipeline import build_pipeline_graph

        params = inspect.signature(build_pipeline_graph).parameters
        if "node_timeout" not in params:
            return
        if build_pipeline_graph.__kwdefaults__ and "node_timeout" in (
            build_pipeline_graph.__kwdefaults__ or {}
        ):
            build_pipeline_graph.__kwdefaults__["node_timeout"] = timeout_s
        logger.info("SEED per-node timeout set to %ds", timeout_s)
    except Exception:  # pragma: no cover - defensive
        logger.warning("Could not adjust SEED node_timeout", exc_info=True)


def synthesize(
    job: SynthesisJob, *, status_cb: Optional[StatusCallback] = None
) -> SynthesisResult:
    """Generate labeled documents for ``job``, reporting progress via ``status_cb``.

    Raises :class:`RuntimeError` (with :data:`INSTALL_HINT`) if the generator is
    not installed - callers should check :func:`generator_available` first and
    degrade gracefully rather than relying on this exception.

    Calls the SEED generator's ``Generator.generate_batch`` (diversity driven by
    ``job.extra`` as the scenario) and shapes the batch result into the IDP
    test-set ``input/`` + ``baseline/<pdf>/sections/<N>/result.json`` layout
    under ``job.out_dir``.
    """

    def _report(pct: float, msg: str) -> None:
        logger.info("synthesis %.0f%%: %s", pct, msg)
        if status_cb is not None:
            try:
                status_cb(pct, msg)
            except Exception:  # pragma: no cover - status is best-effort
                logger.debug("status_cb raised; ignoring", exc_info=True)

    available, reason = generator_available()
    if not available:
        raise RuntimeError(f"{INSTALL_HINT} (import error: {reason})")

    _report(5.0, f"Starting generation of {job.count} document(s)")

    from seed_data import Generator, ModelConfig

    batch_out = os.path.join(job.out_dir, "_batch")
    model_key = _seed_model_key(job.model_id) if job.model_id else None
    model_kwargs = {"data": model_key, "doc": model_key} if model_key else {}

    # Share one boto3 Session across SEED's concurrent workers. Without it SEED
    # creates a fresh Session per worker thread, which races botocore's
    # credential resolver under fan-out (NoCredentialsError in containers).
    import boto3

    _raise_seed_node_timeout()

    generator = Generator(
        models=ModelConfig(**model_kwargs),
        threshold=job.threshold,
        output_dir=batch_out,
        augment=job.augment,
        session=boto3.Session(),
        timeout=_seed_pipeline_timeout_s(),
    )

    usage = SynthesisUsage()

    # SEED fires on_document(index, total, GeneratedDoc) as each result lands;
    # map it onto our 5-80% progress band and accumulate what the document cost.
    def _on_document(index: int, total: int, doc: Any) -> None:
        pct = 5.0 + 75.0 * (index / max(total, 1))
        _report(pct, f"Generated {index}/{total} document(s)")
        _accumulate_usage(usage, doc)

    result = generator.generate_batch(
        job.schema_dir,
        count=job.count,
        scenario=job.extra or "",
        on_document=_on_document,
    )

    documents = list(result.succeeded)
    succeeded = len(documents)
    _report(80.0, f"Generated {succeeded}/{job.count}; shaping into test-set layout")

    if succeeded == 0:
        return SynthesisResult(
            success=False,
            docs_completed=0,
            docs_requested=job.count,
            error="Generator produced no successful documents",
            # Failed runs still burned tokens; excluding them would bias any
            # estimate calibrated on this data toward optimism.
            usage=usage,
        )

    packet_dir = _shape_batch_to_packet(documents, job)
    _report(95.0, "Test-set packet layout written")
    logger.info(
        "synthesis usage: %s tokens over %s document(s), %s pipeline attempts",
        usage.total_tokens,
        usage.docs_measured,
        usage.attempts,
    )
    return SynthesisResult(
        success=True,
        packet_dir=packet_dir,
        docs_completed=succeeded,
        docs_requested=job.count,
        usage=usage,
    )


def _accumulate_usage(usage: SynthesisUsage, doc: Any) -> None:
    """Fold one GeneratedDoc's reported cost into the run total.

    Best-effort and defensive: the generator is a third-party package, so a
    renamed field must degrade the estimate rather than fail the run.
    """
    try:
        tokens = getattr(doc, "token_usage", None) or {}
        usage.input_tokens += int(tokens.get("inputTokens", 0) or 0)
        usage.output_tokens += int(tokens.get("outputTokens", 0) or 0)
        usage.total_tokens += int(tokens.get("totalTokens", 0) or 0)
        usage.attempts += len(getattr(doc, "execution_order", None) or [])
        score = getattr(doc, "score", None)
        if isinstance(score, int):
            usage.scores.append(score)
        usage.docs_measured += 1
    except Exception as e:  # noqa: BLE001 - usage is telemetry, never load-bearing
        logger.debug("Could not read usage from generated document: %s", e)


def _shape_batch_to_packet(documents: List[Any], job: SynthesisJob) -> str:
    import json
    import shutil

    doc_class = _document_class_from_schema_dir(job.schema_dir)
    packet_dir = job.out_dir
    input_dir = os.path.join(packet_dir, "input")
    os.makedirs(input_dir, exist_ok=True)

    for idx, doc in enumerate(documents, start=1):
        # doc is a seed_data GeneratedDoc: typed attrs, with .data lazily
        # loading the paired ground-truth label from data_json_path.
        src_pdf = doc.augmented_path or doc.pdf_path
        if not src_pdf or not os.path.isfile(src_pdf):
            continue
        pdf_name = f"doc_{idx:04d}.pdf"
        shutil.copyfile(src_pdf, os.path.join(input_dir, pdf_name))

        inference_result = doc.data or {}

        page_indices = _pdf_page_indices(os.path.join(input_dir, pdf_name))
        section = {
            "document_class": {"type": doc_class},
            "split_document": {"page_indices": page_indices},
            "inference_result": inference_result,
        }
        sect_dir = os.path.join(packet_dir, "baseline", pdf_name, "sections", "1")
        os.makedirs(sect_dir, exist_ok=True)
        with open(os.path.join(sect_dir, "result.json"), "w", encoding="utf-8") as fh:
            json.dump(section, fh, indent=2)

    return packet_dir


def _document_class_from_schema_dir(schema_dir: str) -> str:
    import glob
    import json

    json_files = glob.glob(os.path.join(schema_dir, "*.json"))
    if json_files:
        try:
            with open(json_files[0], "r", encoding="utf-8") as fh:
                schema = json.load(fh)
            return (
                schema.get("title")
                or schema.get("x-aws-idp-document-type")
                or schema.get("$id")
                or "Document"
            )
        except Exception:
            pass
    return "Document"


def _pdf_page_indices(pdf_path: str) -> List[int]:
    try:
        import fitz

        with fitz.open(pdf_path) as doc:
            return list(range(doc.page_count))
    except Exception:
        return [0]


def estimate_cost(count: int, threshold: int = 7) -> Dict[str, Any]:
    """Rough cost/time estimate for a batch, for UI confirmation prompts.

    Based on measured figures: ~7 min / ~$1.72 per doc at threshold 7; quality
    loops at higher thresholds can be far more expensive, so we widen the band.
    """
    per_doc_usd = 1.75 if threshold <= 7 else 4.0
    per_doc_min = 7.0 if threshold <= 7 else 12.0
    return {
        "documents": count,
        "estimated_usd_low": round(per_doc_usd * count, 2),
        "estimated_usd_high": round(per_doc_usd * count * 2.5, 2),
        "estimated_minutes_low": round(per_doc_min * count / max(1, min(count, 3)), 1),
        "estimated_minutes_high": round(per_doc_min * count, 1),
        "note": "Estimates; actual cost depends on document complexity and retries.",
    }
