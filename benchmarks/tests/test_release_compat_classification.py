# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cross-release A/B must run the cell's classification on the previous release.

Config format 0.9 nests classification settings by model call. The release A/B
uploads one generated file to the previous published release as well as to develop,
and a release that predates the nesting drops every nested block without an error:
a ``sectionSplitting: disabled`` cell silently runs ``llm_determined`` on the code
default model with an empty task prompt. Two halves are pinned here:

* ``compat/classification_flat.py`` adds the equivalent pre-0.9 flat keys, and the
  CURRENT reader resolves the result to the same combined classification as the
  nested original (a flat key wins over a nested one), so one file still serves
  both sides;
* ``compat/release_model_check.py`` loads the TARGET release's own models and turns
  every dropped classification key into an error, and ``run_matrix`` stops on one.
"""

from __future__ import annotations

import copy
import os
import subprocess  # nosec B404 - probes a local git tag
import sys

import pytest
import yaml
from harness_import import harness_module

_COMPAT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "harness", "compat")
if _COMPAT not in sys.path:
    sys.path.insert(0, _COMPAT)

import classification_flat  # noqa: E402
import release_model_check  # noqa: E402

make_configs = harness_module("make_configs")

from idp_common.config.migrations import migrate_config  # noqa: E402
from idp_common.config.models import IDPConfig  # noqa: E402

PREVIOUS_RELEASE = "v0.6.12"
# The first release whose own models declare the nested v0.9 blocks.
NESTED_RELEASE = "v0.6.13"


def _cell_config(cell_id, **axes):
    matrix = yaml.safe_load(open(make_configs.CFG_MATRIX))
    known = {c["id"]: c for c in matrix["core_cells"]}
    cell = dict(known[cell_id])
    cell.update(axes)
    cfg, _ = make_configs.build_cell(
        make_configs.BASE_CONFIG["bank_statement"],
        matrix["axes"],
        dict(matrix["default_cell"]),
        cell,
    )
    return cfg


def _packet(cfg):
    cfg = copy.deepcopy(cfg)
    cfg["classification"]["combined"]["granularity"] = "packet"
    return cfg


def _resolved(cfg):
    return IDPConfig(**migrate_config(copy.deepcopy(cfg))).classification.model_dump()


def _diff(a, b, path=()):
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b)):
            out += _diff(a.get(key), b.get(key), path + (key,))
        return out
    return [".".join(path)] if a != b else []


_PACKET_PAGE_PROMPTS = ["combined.page.system_prompt", "combined.page.task_prompt"]

# (id, config builder, the diff OUTSIDE split_then_classify the current reader may
# show).
# The page-granularity cells must resolve identically everywhere; at packet
# granularity the old release's one prompt pair is the packet pair, which the
# current reader files under combined.page as well — a block a packet-granularity
# run does not read.
CELLS = [
    ("split-disabled", lambda: _cell_config("split-disabled"), []),
    ("split-llm", lambda: _cell_config("split-llm"), []),
    (
        "confidence-off",
        lambda: _cell_config("core-tt-simple-sep", class_confidence="off"),
        [],
    ),
    (
        "confidence-topk",
        lambda: _cell_config("core-tt-simple-sep", class_confidence="topk"),
        [],
    ),
    (
        "classification-model-haiku45",
        lambda: _cell_config("core-tt-simple-sep", classification_model="haiku45"),
        [],
    ),
    (
        "split-disabled-packet",
        lambda: _packet(_cell_config("split-disabled")),
        _PACKET_PAGE_PROMPTS,
    ),
    (
        "split-llm-packet",
        lambda: _packet(_cell_config("split-llm")),
        _PACKET_PAGE_PROMPTS,
    ),
]


@pytest.fixture(scope="module")
def built():
    return {cid: build() for cid, build, _ in CELLS}


@pytest.mark.parametrize(
    "cid,expected_outside_split_then_classify", [(c[0], c[2]) for c in CELLS]
)
def test_current_reader_resolves_the_compat_copy_like_the_original(
    built, cid, expected_outside_split_then_classify
):
    original = built[cid]
    compat, _ = classification_flat.with_flat_classification(original)
    diff = _diff(_resolved(original), _resolved(compat))
    outside = [p for p in diff if not p.startswith("split_then_classify.")]
    assert outside == expected_outside_split_then_classify, (
        f"{cid}: the compat copy changes what a combined run reads: {outside}"
    )


def test_a_split_then_classify_model_of_its_own_is_kept_under_the_flat_model(built):
    """The cell sets only the combined call's model, so the split-then-classify
    calls keep the defaults' model. The flat `model` in the compat copy replaces a
    split-then-classify model only where that call followed the combined model, so
    the copy resolves to exactly the original — pinned, not assumed."""
    original = built["classification-model-haiku45"]
    compat, _ = classification_flat.with_flat_classification(original)
    assert _diff(_resolved(original), _resolved(compat)) == []
    assert _resolved(compat)["strategy"] == "combined"


def test_flat_keys_carry_the_cell_settings(built):
    compat, notes = classification_flat.with_flat_classification(
        built["split-disabled"]
    )
    flat = compat["classification"]
    nested = built["split-disabled"]["classification"]
    assert flat["sectionSplitting"] == "disabled"
    assert flat["classificationMethod"] == "multimodalPageLevelClassification"
    assert flat["model"] == nested["combined"]["llm"]["model"]
    assert flat["task_prompt"] == nested["combined"]["page"]["task_prompt"]
    assert flat["task_prompt"].strip(), (
        "an empty flat prompt is what the bug looked like"
    )
    assert flat["invalidClassFallback"] == nested["fallback_class"]
    assert notes == []
    # The input is not modified.
    assert "classificationMethod" not in built["split-disabled"]["classification"]


def test_packet_granularity_uses_the_packet_prompts_and_reports_what_differs(built):
    original = built["split-llm-packet"]
    compat, notes = classification_flat.with_flat_classification(original)
    flat = compat["classification"]
    packet = original["classification"]["combined"]["packet"]
    assert flat["classificationMethod"] == "textbasedHolisticClassification"
    assert flat["task_prompt"] == packet["task_prompt"]
    assert flat["system_prompt"] == packet["system_prompt"]
    assert any("window_pages" in n for n in notes)
    assert any("confidence" in n for n in notes)


def test_split_then_classify_cell_is_refused(built):
    cfg = copy.deepcopy(built["split-llm"])
    cfg["classification"]["strategy"] = "split_then_classify"
    with pytest.raises(classification_flat.CompatError, match="split-then-classify"):
        classification_flat.with_flat_classification(cfg)


def test_explicit_modality_the_old_release_cannot_express_is_refused(built):
    cfg = copy.deepcopy(built["split-llm"])
    cfg["classification"]["combined"]["llm"]["modality"]["images"] = False
    with pytest.raises(classification_flat.CompatError, match="modality.images"):
        classification_flat.with_flat_classification(cfg)


# --- dropped_keys, against synthetic models (no git tag needed) ---------------


def test_dropped_keys_walks_sub_models_and_stops_at_free_form_fields():
    from typing import Any, Dict, Optional

    from pydantic import BaseModel, ConfigDict

    class Leaf(BaseModel):
        mode: str = "topk"

    class Open(BaseModel):
        model_config = ConfigDict(extra="allow")

    class Root(BaseModel):
        model: str = ""
        confidence: Leaf = Leaf()
        maybe: Optional[Leaf] = None
        free: Dict[str, Any] = {}
        open_block: Open = Open()

    data = {
        "model": "m",
        "combined": {"llm": {}},
        "confidence": {"mode": "off", "task_prompt_topk_packet": "x"},
        "maybe": {"nope": 1},
        "free": {"anything": 1},
        "open_block": {"kept": 1},
    }
    assert release_model_check.dropped_keys(Root, data, "classification") == [
        "classification.combined",
        "classification.confidence.task_prompt_topk_packet",
        "classification.maybe.nope",
    ]


# --- against the previous release's real models -------------------------------


def _tag_available(tag):
    probe = subprocess.run(  # nosec B603 B607 - fixed argv
        ["git", "-C", make_configs.REPO, "rev-parse", "--verify", "--quiet", tag],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


needs_release = pytest.mark.skipif(
    not _tag_available(PREVIOUS_RELEASE),
    reason=f"git tag {PREVIOUS_RELEASE} is not in this clone (git fetch --tags)",
)


@pytest.fixture(scope="module")
def release_lib():
    return release_model_check.extract_release_lib(PREVIOUS_RELEASE)


@needs_release
def test_nested_config_fails_against_the_previous_release(built, release_lib):
    errors = release_model_check.check_config(built["split-disabled"], release_lib)
    dropped = {e.split(" ")[0] for e in errors}
    assert {
        "classification.combined",
        "classification.document_shape",
        "classification.fallback_class",
    } <= dropped


@needs_release
@pytest.mark.parametrize(
    "cid", ["split-disabled", "split-llm-packet", "confidence-off"]
)
def test_compat_copy_passes_against_the_previous_release(built, release_lib, cid):
    compat, _ = classification_flat.with_flat_classification(built[cid])
    assert release_model_check.check_config(compat, release_lib) == []


@needs_release
def test_a_flat_value_the_release_resolves_differently_fails(built, release_lib):
    compat, _ = classification_flat.with_flat_classification(built["split-disabled"])
    compat["classification"]["sectionSplitting"] = "llm_determined"
    errors = release_model_check.check_config(compat, release_lib)
    assert any("classification.sectionSplitting" in e for e in errors), errors


@needs_release
def test_a_flat_key_the_release_does_not_declare_fails(built, release_lib):
    compat, _ = classification_flat.with_flat_classification(built["split-disabled"])
    compat["classification"]["granularity"] = "page"
    errors = release_model_check.check_config(compat, release_lib)
    assert any(e.startswith("classification.granularity ") for e in errors), errors


# --- against the first release that reads the nested layout (#1674) ----------

needs_nested_release = pytest.mark.skipif(
    not _tag_available(NESTED_RELEASE),
    reason=f"git tag {NESTED_RELEASE} is not in this clone (git fetch --tags)",
)


@pytest.fixture(scope="module")
def nested_release_lib():
    return release_model_check.extract_release_lib(NESTED_RELEASE)


@needs_release
@needs_nested_release
def test_only_the_nested_release_reads_the_nested_layout(
    release_lib, nested_release_lib
):
    assert release_model_check.reads_nested_classification(release_lib) is False
    assert release_model_check.reads_nested_classification(nested_release_lib) is True


@needs_nested_release
@pytest.mark.parametrize(
    "cid", ["split-disabled", "split-llm-packet", "confidence-off"]
)
def test_nested_config_passes_against_the_nested_release(
    built, nested_release_lib, cid
):
    assert release_model_check.check_config(built[cid], nested_release_lib) == []


@needs_nested_release
def test_a_nested_key_the_nested_release_does_not_declare_fails(
    built, nested_release_lib
):
    cfg = copy.deepcopy(built["split-disabled"])
    cfg["classification"]["document_shape"]["no_such_key"] = 1
    errors = release_model_check.check_config(cfg, nested_release_lib)
    assert any(
        e.startswith("classification.document_shape.no_such_key ") for e in errors
    ), errors


@needs_nested_release
def test_the_flat_copy_is_refused_against_the_nested_release_in_one_line(
    built, nested_release_lib
):
    compat, _ = classification_flat.with_flat_classification(built["split-disabled"])
    errors = release_model_check.check_config(compat, nested_release_lib)
    assert len(errors) == 1 and "flat key" in errors[0], errors


@needs_nested_release
@pytest.mark.parametrize(
    "key,value",
    [
        ("model", "us.amazon.nova-micro-v1:0"),
        ("temperature", 0.9),
        ("sectionSplitting", "page"),
        ("task_prompt", "OVERRIDE"),
    ],
)
def test_a_single_flat_key_is_refused_against_the_nested_release(
    built, nested_release_lib, key, value
):
    """Without classificationMethod the release still converts the key on read and
    uses it instead of the nested value, and it never shows up as dropped."""
    cfg = copy.deepcopy(built["split-disabled"])
    cfg["classification"][key] = value
    errors = release_model_check.check_config(cfg, nested_release_lib)
    assert len(errors) == 1 and key in errors[0], errors


@needs_nested_release
def test_prepare_native_configs_passes_against_the_nested_release(built, tmp_path):
    """The shape #1674 reported: the run stopped on every cell."""
    pytest.importorskip("boto3")
    run_matrix = harness_module("run_matrix")
    cells = [
        _write_cell(tmp_path, "bench-split-disabled", built["split-disabled"]),
        _write_cell(tmp_path, "bench-packet", built["split-llm-packet"]),
    ]
    prepared, problems = run_matrix.prepare_native_configs(cells, NESTED_RELEASE)
    assert problems == []
    assert prepared["bench-split-disabled"] == built["split-disabled"]
    assert prepared["bench-packet"] == built["split-llm-packet"]


# --- run_matrix stops on a problem --------------------------------------------


def _write_cell(tmp_path, version, cfg):
    path = tmp_path / f"{version}.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False))
    return {"version": version, "path": str(path)}


def test_prepare_native_configs_reports_problems_and_prepares_the_rest(
    built, tmp_path, monkeypatch
):
    pytest.importorskip("boto3")
    run_matrix = harness_module("run_matrix")
    split_then_classify = copy.deepcopy(built["split-llm"])
    split_then_classify["classification"]["strategy"] = "split_then_classify"
    cells = [
        _write_cell(tmp_path, "bench-ok", built["split-disabled"]),
        _write_cell(tmp_path, "bench-split", split_then_classify),
        _write_cell(tmp_path, "bench-dropped", built["split-llm"]),
    ]
    monkeypatch.setattr(release_model_check, "extract_release_lib", lambda tag: "/x")
    monkeypatch.setattr(
        release_model_check, "reads_nested_classification", lambda lib_root: False
    )

    def fake_check(cfg, lib_root):
        # The prepared copy is what gets checked; fail one version only.
        assert "classificationMethod" in cfg["classification"]
        if cfg["classification"]["sectionSplitting"] == "llm_determined":
            return ["classification.x is not read by this release"]
        return []

    monkeypatch.setattr(release_model_check, "check_config", fake_check)
    prepared, problems = run_matrix.prepare_native_configs(cells, PREVIOUS_RELEASE)
    assert sorted(prepared) == ["bench-dropped", "bench-ok"]
    assert len(problems) == 2
    assert any(
        p.startswith("bench-split:") and "split-then-classify" in p for p in problems
    )
    assert any(p.startswith("bench-dropped:") for p in problems)


def test_prepare_native_configs_uploads_the_file_unchanged_to_a_nested_reader(
    built, tmp_path, monkeypatch
):
    """#1674: a release that reads the nested blocks (v0.6.13 onward) gets the
    cell's file as generated. Adding the flat keys there made every cell fail the
    check, and would have let the flat keys override the nested blocks on read."""
    pytest.importorskip("boto3")
    run_matrix = harness_module("run_matrix")
    split_then_classify = copy.deepcopy(built["split-llm"])
    split_then_classify["classification"]["strategy"] = "split_then_classify"
    cells = [
        _write_cell(tmp_path, "bench-ok", built["split-disabled"]),
        _write_cell(tmp_path, "bench-split", split_then_classify),
    ]
    monkeypatch.setattr(release_model_check, "extract_release_lib", lambda tag: "/x")
    monkeypatch.setattr(
        release_model_check, "reads_nested_classification", lambda lib_root: True
    )
    checked = []

    def fake_check(cfg, lib_root):
        checked.append(cfg)
        return []

    monkeypatch.setattr(release_model_check, "check_config", fake_check)
    prepared, problems = run_matrix.prepare_native_configs(cells, NESTED_RELEASE)
    assert problems == []
    # A split-then-classify cell is not refused: this release has the strategy.
    assert prepared == {
        "bench-ok": built["split-disabled"],
        "bench-split": split_then_classify,
    }
    assert checked == list(prepared.values())


# --- reads_nested_classification, against synthetic release trees -------------


def _fake_release(root, classification_fields):
    """A minimal ``idp_common`` whose ClassificationConfig declares the given
    fields; ``"name:alias"`` declares ``name`` under ``alias``."""
    pkg = root / "idp_common"
    (pkg / "config").mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "config" / "__init__.py").write_text("")
    body = "\n".join(
        f"    {name}: object = None"
        if ":" not in name
        else f"    {name.split(':')[0]}: object = Field(None, alias={name.split(':')[1]!r})"
        for name in classification_fields
    )
    (pkg / "config" / "models.py").write_text(
        "from pydantic import BaseModel, Field\n\n"
        "class ClassificationConfig(BaseModel):\n"
        f"{body or '    pass'}\n\n"
        "class IDPConfig(BaseModel):\n"
        "    classification: ClassificationConfig = ClassificationConfig()\n"
    )
    return str(root)


@pytest.mark.parametrize(
    "fields,expected",
    [
        (list(classification_flat.V09_BLOCKS) + ["model"], True),
        (["model", "classificationMethod", "sectionSplitting"], False),
        # Only some of the blocks: not a nested reader, so the flat keys are kept
        # and check_config reports what the release drops.
        ([b for b in classification_flat.V09_BLOCKS if b != "document_shape"], False),
        # A block declared under an alias is read like the field name.
        (
            [b for b in classification_flat.V09_BLOCKS if b != "document_shape"]
            + ["shape:document_shape"],
            True,
        ),
    ],
    ids=["nested", "flat", "partial", "alias"],
)
def test_reads_nested_classification_reads_the_target_release_model(
    tmp_path, fields, expected
):
    lib_root = _fake_release(tmp_path, fields)
    assert release_model_check.reads_nested_classification(lib_root) is expected


def test_classification_fields_refuses_a_tree_without_idp_common(tmp_path):
    # Either nothing imports, or an installed idp_common from elsewhere does and
    # the provenance check refuses it; both must raise rather than answer.
    with pytest.raises(
        RuntimeError, match="cannot read the classification model|resolved to"
    ):
        release_model_check.classification_fields(str(tmp_path))


def test_native_upload_refuses_an_unprepared_config():
    pytest.importorskip("boto3")
    run_matrix = harness_module("run_matrix")
    with pytest.raises(ValueError, match="prepare_native_configs"):
        run_matrix.upload_config("stk", "bench-a", "/nope.yaml", res={}, native=True)


def test_native_upload_without_a_release_to_validate_against_is_an_error(monkeypatch):
    pytest.importorskip("boto3")
    run_matrix = harness_module("run_matrix")
    monkeypatch.setattr(
        sys, "argv", ["run_matrix.py", "--stack", "s", "--native-upload"]
    )
    monkeypatch.setattr(
        run_matrix,
        "resolve_stack",
        lambda *_: pytest.fail("argument check must come before any AWS call"),
    )
    with pytest.raises(SystemExit) as exc:
        run_matrix.main()
    assert exc.value.code == 2
