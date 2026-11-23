#!/usr/bin/env python3
"""Measure section classification by embeddings against the LLM, and fit its settings.

``split_then_classify.section_classification.method: embeddings`` (GitHub #1657)
labels a section by comparing its page images with reference vectors per class and
sends the sections it is unsure of to the LLM. Its thresholds cannot be given honest
values without a measurement, and this driver is that measurement. It runs the
PRODUCT's own code — ``ClassificationService._classify_segments`` with the
``embeddings`` and ``llm`` methods, over real Bedrock — on single-class documents
whose class is known, so the boundaries are given and only the label is scored
(the quantity the evaluation reports as ``label_accuracy``).

The corpus is a directory with one sub-directory of PDFs per class, e.g. the
RVL-CDIP-N multi-page source documents the DocSplit test set is built from
(``src/lambda/docsplit_testset_deployer``), extracted outside this tree. Class
names are the folder names with spaces as underscores, matching the ``docsplit``
preset, whose classes, descriptions and LLM settings the run uses.

The split is BY DOCUMENT and stratified by class, with a fixed seed: per class,
``--exemplars`` documents become the class's reference images (their first page, as
``x-aws-idp-examples``), ``--calibration`` documents fit the temperature and choose
the thresholds, and the rest are the test split every reported figure comes from.
No document is on two sides, so accuracy is not inflated by matching a page against
itself.

Every Bedrock and Textract response is cached on disk under ``--cache`` (outside the
tree), keyed on the request, so a second run replays the first offline and costs
nothing; the cache also records the billed input tokens. Textract supplies the OCR
text the LLM arm reads, since the source PDFs are scans with no text layer.

Usage:
  AWS_PROFILE=default AWS_REGION=us-east-1 python3 benchmarks/harness/embedding_labeling_bench.py \
      --corpus <dir> --cache <dir outside the tree> --per-class 25 --out <file.json>
"""

# ruff: noqa: E402  (local sibling imports require the sys.path bootstrap first)
import argparse
import copy
import hashlib
import json
import math
import os
import random
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import yaml

BENCH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(BENCH)
sys.path.insert(0, os.path.join(REPO, "lib", "idp_common_pkg"))

from idp_common import image as idp_image
from idp_common import s3 as idp_s3
from idp_common.bedrock.client import BedrockClient
from idp_common.classification import embeddings_labeler as el
from idp_common.classification.models import Segment
from idp_common.classification.service import ClassificationService
from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.models import Document, Page

PRESET = os.path.join(REPO, "config_library", "unified", "docsplit", "config.yaml")
LOCAL = "local://"

# ── disk cache over the two Bedrock calls and Textract ────────────────────────
_lock = threading.Lock()


class DiskCache:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(root, exist_ok=True)
        self.offline = False
        self.hits = 0
        self.misses = 0
        self.embedding_tokens = 0
        self.textract_pages = 0

    def _path(self, kind: str, key: str) -> str:
        return os.path.join(self.root, kind, key[:2], key + ".json")

    def get(self, kind: str, key: str) -> Optional[Any]:
        path = self._path(kind, key)
        if os.path.exists(path):
            with open(path) as f:
                with _lock:
                    self.hits += 1
                return json.load(f)
        if self.offline:
            raise SystemExit(f"--offline: the cache holds no {kind} response {key}")
        return None

    def put(self, kind: str, key: str, value: Any) -> None:
        path = self._path(kind, key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + f".{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w") as f:
            json.dump(value, f)
        os.replace(tmp, path)
        with _lock:
            self.misses += 1


def _digest(*parts: Any) -> str:
    h = hashlib.sha256()
    for part in parts:
        if isinstance(part, bytes):
            h.update(part)
        else:
            h.update(json.dumps(part, sort_keys=True, default=str).encode())
        h.update(b"\x00")
    return h.hexdigest()


def install_cache(cache: DiskCache) -> None:
    """Route the product's Bedrock embedding and converse calls through the cache."""
    original_embed = BedrockClient.generate_embedding
    original_invoke = BedrockClient.invoke_model

    def generate_embedding(
        self,
        text=None,
        model_id="",
        max_retries=None,
        image_bytes=None,
        input_type="search_document",
        usage=None,
    ):
        key = _digest("embed", model_id, input_type, text, image_bytes or b"")
        hit = cache.get("embed", key)
        if hit is None:
            u: Dict[str, int] = {}
            vector = original_embed(
                self,
                text=text,
                model_id=model_id,
                max_retries=max_retries,
                image_bytes=image_bytes,
                input_type=input_type,
                usage=u,
            )
            hit = {"vector": vector, "inputTokens": u.get("inputTokens")}
            cache.put("embed", key, hit)
        if usage is not None and hit.get("inputTokens") is not None:
            usage["inputTokens"] = hit["inputTokens"]
        with _lock:
            cache.embedding_tokens += int(hit.get("inputTokens") or 0)
        return hit["vector"]

    def invoke_model(self, *args, **kwargs):
        key = _digest(
            "converse",
            kwargs.get("model_id"),
            kwargs.get("system_prompt"),
            _strip_bytes(kwargs.get("content")),
            kwargs.get("temperature"),
            kwargs.get("top_k"),
            kwargs.get("max_tokens"),
        )
        hit = cache.get("converse", key)
        if hit is None:
            hit = original_invoke(self, *args, **kwargs)
            cache.put("converse", key, _jsonable(hit))
        return hit

    BedrockClient.generate_embedding = generate_embedding
    BedrockClient.invoke_model = invoke_model


def _strip_bytes(node: Any) -> Any:
    if isinstance(node, bytes):
        return hashlib.sha256(node).hexdigest()
    if isinstance(node, dict):
        return {k: _strip_bytes(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_strip_bytes(v) for v in node]
    return node


def _jsonable(node: Any) -> Any:
    return json.loads(json.dumps(node, default=str))


def install_local_io() -> None:
    """Let page ``image_uri`` and ``parsed_text_uri`` name local files."""
    original_prepare = idp_image.prepare_image
    original_text = idp_s3.get_text_content

    def prepare_image(source, *args, **kwargs):
        if isinstance(source, str) and source.startswith(LOCAL):
            with open(source[len(LOCAL) :], "rb") as f:
                source = f.read()
        return original_prepare(source, *args, **kwargs)

    def get_text_content(uri, *args, **kwargs):
        if isinstance(uri, str) and uri.startswith(LOCAL):
            with open(uri[len(LOCAL) :]) as f:
                return f.read()
        return original_text(uri, *args, **kwargs)

    idp_image.prepare_image = prepare_image
    idp_s3.get_text_content = get_text_content


# ── corpus ────────────────────────────────────────────────────────────────────
def render_pages(pdf_path: str, out_dir: str, max_pages: int) -> List[str]:
    import pypdfium2 as pdfium

    stem = os.path.join(out_dir, _digest(pdf_path)[:16])
    paths = [f"{stem}_p{i + 1}.jpg" for i in range(max_pages)]
    existing = [p for p in paths if os.path.exists(p)]
    if existing and os.path.exists(stem + ".done"):
        return existing
    pdf = pdfium.PdfDocument(pdf_path)
    written = []
    for i in range(min(max_pages, len(pdf))):
        pil = pdf[i].render(scale=150 / 72).to_pil().convert("RGB")
        pil.save(paths[i], format="JPEG", quality=85)
        written.append(paths[i])
    pdf.close()
    open(stem + ".done", "w").close()
    return written


def ocr_text(cache: DiskCache, image_path: str, region: str) -> str:
    import boto3

    with open(image_path, "rb") as f:
        data = f.read()
    key = _digest("textract", data)
    hit = cache.get("textract", key)
    if hit is None:
        client = boto3.client("textract", region_name=region)
        response = client.detect_document_text(Document={"Bytes": data})
        lines = [
            b["Text"] for b in response.get("Blocks", []) if b["BlockType"] == "LINE"
        ]
        hit = {"text": "\n".join(lines)}
        cache.put("textract", key, hit)
        with _lock:
            cache.textract_pages += 1
    return hit["text"]


_UNREADABLE: List[str] = []


def _readable(pdf_path: str) -> bool:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(pdf_path)
        ok = len(pdf) > 0
        pdf.close()
        return ok
    except Exception:
        return False


def split_corpus(
    corpus: str, per_class: int, exemplars: int, calibration: int, seed: int
) -> Dict[str, List[Tuple[str, str]]]:
    rng = random.Random(seed)
    splits: Dict[str, List[Tuple[str, str]]] = {
        "exemplar": [],
        "calibration": [],
        "test": [],
    }
    for folder in sorted(os.listdir(corpus)):
        path = os.path.join(corpus, folder)
        if not os.path.isdir(path):
            continue
        name = folder.replace(" ", "_")
        docs = sorted(f for f in os.listdir(path) if f.lower().endswith(".pdf"))
        # An unreadable PDF is dropped before the split, so it shifts no document
        # between sides; the count dropped is reported.
        readable = [d for d in docs if _readable(os.path.join(path, d))]
        _UNREADABLE.extend(os.path.join(folder, d) for d in docs if d not in readable)
        docs = readable
        rng.shuffle(docs)
        docs = docs[:per_class]
        if len(docs) < 3:
            raise SystemExit(f"class {name} has only {len(docs)} documents")
        # A class with fewer documents than asked for keeps every split populated:
        # at most a quarter of its documents become exemplars, and the rest are
        # halved between calibration and test (test takes the odd one).
        n_ex = min(exemplars, max(1, len(docs) // 4))
        n_cal = min(calibration, (len(docs) - n_ex) // 2)
        for i, doc in enumerate(docs):
            side = (
                "exemplar"
                if i < n_ex
                else "calibration"
                if i < n_ex + n_cal
                else "test"
            )
            splits[side].append((name, os.path.join(path, doc)))
    return splits


# ── the product's labeling, on a synthetic document holding one segment per doc ─
def base_config() -> Dict[str, Any]:
    with open(PRESET) as f:
        preset = yaml.safe_load(f)
    merged = merge_config_with_defaults(preset)
    merged["classification"]["strategy"] = "split_then_classify"
    return merged


def labeled_config(
    merged: Dict[str, Any],
    method: str,
    settings: Dict[str, Any],
    exemplar_pages: Dict[str, List[str]],
) -> Dict[str, Any]:
    cfg = copy.deepcopy(merged)
    section = cfg["classification"]["split_then_classify"]["section_classification"]
    section["method"] = method
    if method == "embeddings":
        section["embeddings"] = {
            k: v
            for k, v in settings.items()
            if k
            in (
                "model",
                "min_similarity",
                "references",
                "temperature",
                "max_image_dimension",
            )
        }
        section["low_confidence"] = {
            "min_confidence": settings.get("min_confidence", 0.0),
            "on_low_confidence": "llm",
        }
        if not section["low_confidence"]["min_confidence"] and not section[
            "embeddings"
        ].get("min_similarity"):
            # The validator refuses a configuration that can never fall back. The
            # sweep wants every embeddings answer, so it uses a threshold no
            # probability is below.
            section["low_confidence"]["min_confidence"] = 1e-9
    for schema in cfg["classes"]:
        name = schema.get("x-aws-idp-document-type")
        schema.pop("x-aws-idp-examples", None)
        if method == "embeddings" and name in exemplar_pages:
            schema["x-aws-idp-examples"] = [
                {"name": f"{name}-{i}", "x-aws-idp-image-path": path}
                for i, path in enumerate(exemplar_pages[name])
            ]
    return cfg


def build_document(
    docs: List[Tuple[str, List[str], List[str]]],
) -> Tuple[Document, List[Segment], List[str]]:
    pages: Dict[str, Page] = {}
    segments: List[Segment] = []
    truth: List[str] = []
    n = 0
    for name, images, texts in docs:
        ids = []
        for img, txt in zip(images, texts):
            n += 1
            pid = str(n)
            pages[pid] = Page(
                page_id=pid, image_uri=LOCAL + img, parsed_text_uri=LOCAL + txt
            )
            ids.append(pid)
        segments.append(Segment(page_ids=ids))
        truth.append(name)
    document = Document(id="bench", input_key="bench.pdf", pages=pages, num_pages=n)
    return document, segments, truth


def run_labeling(cfg: Dict[str, Any], region: str, docs, workers: int):
    service = ClassificationService(region=region, config=cfg, max_workers=workers)
    document, segments, truth = build_document(docs)
    t0 = time.time()
    labeled, metering, errors, issues = service._classify_segments(document, segments)
    elapsed = time.time() - t0
    rows = []
    for seg, want in zip(labeled, truth):
        reason = document.pages[seg.page_ids[0]].classification_reason or ""
        rows.append(
            {
                "truth": want,
                "predicted": seg.doc_class,
                "confidence": seg.confidence,
                "by_llm": "labeled by the LLM" in reason
                or not reason.startswith("Embeddings"),
                "reason": reason,
            }
        )
    return rows, metering, errors, elapsed


def embedding_scores(cfg: Dict[str, Any], region: str, docs) -> List[Dict[str, Any]]:
    """Per-document similarities from the product's labeler, before any threshold."""
    service = ClassificationService(region=region, config=cfg)
    section = service.config.classification.split_then_classify.section_classification
    labeler = el.EmbeddingSectionLabeler(service, section)
    document, segments, truth = build_document(docs)
    from concurrent.futures import ThreadPoolExecutor

    def one(seg):
        scored = labeler.score(
            document, service._section_classification_pages(seg), "bench"
        )
        return scored.similarities if scored else {}

    with ThreadPoolExecutor(max_workers=8) as pool:
        sims = list(pool.map(one, segments))
    return [{"truth": want, "similarities": s} for want, s in zip(truth, sims)]


# ── statistics over cached similarities ───────────────────────────────────────
def decide(row: Dict[str, Any], temperature: float) -> Tuple[str, float, float, float]:
    sims = row["similarities"]
    probs = el.softmax_probabilities(sims, temperature)
    top = max(probs, key=lambda k: probs[k])
    others = sorted((v for k, v in sims.items() if k != top), reverse=True)
    return top, probs[top], sims[top], sims[top] - (others[0] if others else sims[top])


def accuracy(rows) -> float:
    return sum(r["predicted"] == r["truth"] for r in rows) / len(rows)


def top1(rows, temperature: float) -> float:
    return sum(decide(r, temperature)[0] == r["truth"] for r in rows) / len(rows)


def nll(rows, temperature: float) -> float:
    total = 0.0
    for row in rows:
        p = el.softmax_probabilities(row["similarities"], temperature).get(
            row["truth"], 0.0
        )
        total -= math.log(max(p, 1e-12))
    return total / len(rows)


def fit_temperature(rows) -> float:
    grid = [round(0.002 * k, 3) for k in range(1, 151)]
    return min(grid, key=lambda t: nll(rows, t))


def ece(pairs: List[Tuple[float, bool]], bins: int = 10) -> float:
    total = len(pairs)
    err = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [
            (c, ok) for c, ok in pairs if (lo <= c < hi) or (b == bins - 1 and c == 1.0)
        ]
        if sel:
            err += (
                len(sel)
                / total
                * abs(
                    sum(c for c, _ in sel) / len(sel)
                    - sum(ok for _, ok in sel) / len(sel)
                )
            )
    return err


def hybrid(
    rows, llm_by_index: List[str], temperature: float, min_conf: float, min_sim: float
) -> Dict[str, Any]:
    correct = fallback = accepted_ok = 0
    for row, llm_pred in zip(rows, llm_by_index):
        top, p, s1, _ = decide(row, temperature)
        if p < min_conf or s1 < min_sim:
            fallback += 1
            correct += llm_pred == row["truth"]
        else:
            accepted_ok += top == row["truth"]
            correct += top == row["truth"]
    n = len(rows)
    accepted = n - fallback
    return {
        "accuracy": correct / n,
        "fallback_rate": fallback / n,
        "accepted": accepted,
        "accepted_accuracy": (accepted_ok / accepted) if accepted else None,
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--cache", required=True, help="a directory OUTSIDE the repository")
    ap.add_argument("--out", required=True)
    ap.add_argument("--per-class", type=int, default=25)
    ap.add_argument("--exemplars", type=int, default=5)
    ap.add_argument("--calibration", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1657)
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument(
        "--dimensions", default="512,1024", help="max_image_dimension values to compare"
    )
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--skip-llm", action="store_true")
    ap.add_argument(
        "--offline",
        action="store_true",
        help="replay the cache only: a request it does not hold is an error, not a call",
    )
    args = ap.parse_args()

    if os.path.realpath(args.cache).startswith(os.path.realpath(REPO) + os.sep):
        raise SystemExit(
            "--cache must be outside the repository (content-discovered gates read it)"
        )

    cache = DiskCache(args.cache)
    cache.offline = args.offline
    install_cache(cache)
    install_local_io()
    os.environ["ROOT_DIR"] = "/"

    splits = split_corpus(
        args.corpus, args.per_class, args.exemplars, args.calibration, args.seed
    )
    pages_dir = os.path.join(args.cache, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    def prepared(side: str, max_pages: int, with_text: bool):
        out = []
        for name, pdf in splits[side]:
            images = render_pages(pdf, pages_dir, 2)[:max_pages]
            texts = []
            if with_text:
                for img in images:
                    path = img + ".txt"
                    if not os.path.exists(path):
                        with open(path, "w") as f:
                            f.write(ocr_text(cache, img, args.region))
                    texts.append(path)
            else:
                texts = [img + ".none" for img in images]
            out.append((name, images, texts))
        return out

    exemplar_pages: Dict[str, List[str]] = {}
    for name, pdf in splits["exemplar"]:
        exemplar_pages.setdefault(name, []).append(
            render_pages(pdf, pages_dir, 1)[0].lstrip("/")
        )

    merged = base_config()
    calib = prepared("calibration", 2, with_text=not args.skip_llm)
    test = prepared("test", 2, with_text=not args.skip_llm)
    result: Dict[str, Any] = {
        "corpus_classes": sorted({n for n, _ in splits["test"]}),
        "n": {k: len(v) for k, v in splits.items()},
        "seed": args.seed,
        "per_class": args.per_class,
        "split": "by document, stratified by class",
        "unreadable_pdfs_dropped": list(_UNREADABLE),
        "pages": "section_classification.pages first_k, k=2 (the shipped default)",
    }

    # 1. Embeddings alone, per reference source and image size, on both splits.
    sweeps: Dict[str, Any] = {}
    for references in ("examples", "descriptions"):
        for dim in [int(d) for d in args.dimensions.split(",")]:
            settings = {
                "references": references,
                "max_image_dimension": dim,
                "temperature": 0.02,
            }
            cfg = labeled_config(merged, "embeddings", settings, exemplar_pages)
            rows_c = embedding_scores(cfg, args.region, calib)
            rows_t = embedding_scores(cfg, args.region, test)
            t = fit_temperature(rows_c)
            sweeps[f"{references}@{dim}"] = {
                "temperature": t,
                "calibration_top1": top1(rows_c, t),
                "test_top1": top1(rows_t, t),
                "test_ece": ece(
                    [(decide(r, t)[1], decide(r, t)[0] == r["truth"]) for r in rows_t]
                ),
                "rows_calibration": rows_c,
                "rows_test": rows_t,
            }
            print(
                f"{references}@{dim}: T={t} top1 calib={top1(rows_c, t):.3f} test={top1(rows_t, t):.3f}",
                flush=True,
            )
    result["embeddings_only"] = {
        k: {kk: vv for kk, vv in v.items() if not kk.startswith("rows")}
        for k, v in sweeps.items()
    }

    if args.skip_llm:
        json.dump(result, open(args.out, "w"), indent=2)
        return

    # 2. The LLM method on both splits: the baseline, and the fallback's answers.
    llm_cfg = labeled_config(merged, "llm", {}, exemplar_pages)
    llm_c, llm_c_meter, llm_c_err, _ = run_labeling(
        llm_cfg, args.region, calib, args.workers
    )
    llm_t, llm_t_meter, llm_t_err, llm_t_time = run_labeling(
        llm_cfg, args.region, test, args.workers
    )
    result["llm"] = {
        "model": merged["classification"]["split_then_classify"][
            "section_classification"
        ]["llm"]["model"],
        "calibration_accuracy": accuracy(llm_c),
        "test_accuracy": accuracy(llm_t),
        "errors": llm_c_err + llm_t_err,
        "test_metering": llm_t_meter,
        "test_seconds": round(llm_t_time, 1),
    }
    print(
        f"LLM accuracy calib={accuracy(llm_c):.3f} test={accuracy(llm_t):.3f}",
        flush=True,
    )

    # 3. Thresholds chosen on the calibration split only: the lowest min_confidence
    # at which the hybrid is at least as accurate as the LLM alone there.
    choices = {}
    for key, sweep in sweeps.items():
        t = sweep["temperature"]
        target = accuracy(llm_c)
        grid = [round(0.05 * k, 2) for k in range(0, 20)] + [0.97, 0.98, 0.99]
        chosen = None
        calibration_curve = {
            str(m): hybrid(
                sweep["rows_calibration"], [r["predicted"] for r in llm_c], t, m, 0.0
            )
            for m in grid
        }
        for m in grid:
            if calibration_curve[str(m)]["accuracy"] >= target:
                chosen = m
                break
        curve = {
            str(m): hybrid(
                sweep["rows_test"], [r["predicted"] for r in llm_t], t, m, 0.0
            )
            for m in grid
        }
        choices[key] = {
            "temperature": t,
            "min_confidence": chosen,
            "calibration_at_choice": (
                calibration_curve[str(chosen)] if chosen is not None else None
            ),
            "calibration_curve": calibration_curve,
            "test_at_choice": curve.get(str(chosen)) if chosen is not None else None,
            "test_curve": curve,
        }
    result["hybrid"] = choices

    # 4. The chosen setting end to end through the product, fallbacks live. The
    # configuration is chosen on the CALIBRATION split's fallback rate, like the
    # threshold, so the test split decides nothing it is then used to report.
    best = max(
        (
            k
            for k in choices
            if k.startswith("examples") and choices[k]["min_confidence"] is not None
        ),
        key=lambda k: (
            -(choices[k]["calibration_at_choice"] or {}).get("fallback_rate", 1.0)
        ),
        default=None,
    )
    if best:
        ref, dim = best.split("@")
        settings = {
            "references": ref,
            "max_image_dimension": int(dim),
            "temperature": choices[best]["temperature"],
            "min_confidence": choices[best]["min_confidence"],
        }
        cfg = labeled_config(merged, "embeddings", settings, exemplar_pages)
        rows, meter, errs, seconds = run_labeling(cfg, args.region, test, args.workers)
        result["end_to_end"] = {
            "setting": settings,
            "accuracy": accuracy(rows),
            "fallback_rate": sum(r["by_llm"] for r in rows) / len(rows),
            "errors": errs,
            "metering": meter,
            "seconds": round(seconds, 1),
            "llm_seconds": round(llm_t_time, 1),
        }
        print(
            json.dumps(
                {k: v for k, v in result["end_to_end"].items() if k != "metering"}
            ),
            flush=True,
        )

    result["cache"] = {
        "hits": cache.hits,
        "misses": cache.misses,
        "textract_pages_called": cache.textract_pages,
    }
    with open(args.out, "w") as f:
        json.dump(result, f, indent=2, default=str)


if __name__ == "__main__":
    main()
