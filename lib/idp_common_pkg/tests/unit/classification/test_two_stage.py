# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``classification.strategy: two_stage`` — segmentation, then labeling (#1329).

A single-pass call does two different jobs at once and pays for it twice. Every
boundary decision carries the whole class vocabulary — the class list, every
description and every few-shot example — although finding a boundary does not
depend on any of it, and at page granularity that prefix is paid N times. And
labeling scales with pages rather than documents: a 40-page statement is labeled
forty times to produce one answer.

Two-stage splits them. Stage A reuses granularity, modality, windowing and
context pages with a prompt that asks only where documents start, returning
segments with **no class** — which is the whole reason ``Segment.doc_class`` is
optional. Stage B makes one call per segment.

Three properties here are the ones a refactor could lose without any structural
test noticing:

- **The two halves are metered separately**, under ``Segmentation`` and
  ``SectionClassification``, while single-pass stays ``Classification`` with unchanged units.
  That split is what makes "is this actually cheaper on my documents?"
  answerable from the cost table rather than from arithmetic.
- **Stage A sends no class vocabulary.** If a prompt or substitution change
  quietly reintroduces it, two-stage still works and costs what single-pass
  cost, which is the one failure mode with no symptom.
- **A forced class skips Stage B entirely.** The document name already asserts
  the class, so there is nothing to label.
"""

from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from idp_common.classification.service import ClassificationService
from idp_common.models import Document, Page

SEG_PAGE_PROMPT = (
    "Does a document start here?\n{BOUNDARY_HINTS}\n"
    "<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n"
    "<document-image>\n{DOCUMENT_IMAGE}\n</document-image>\n"
)
SEG_PACKET_PROMPT = (
    "Split this packet into ranges.\n{BOUNDARY_HINTS}\n"
    "<document-text>\n{DOCUMENT_TEXT}\n</document-text>\n"
    "<document-images>\n{DOCUMENT_IMAGE}\n</document-images>\n"
)
LABEL_PROMPT = (
    "<document-types>\n{CLASS_NAMES_AND_DESCRIPTIONS}\n</document-types>\n"
    "<few-shot-examples>\n{FEW_SHOT_EXAMPLES}\n</few-shot-examples>\n"
    "<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n"
    "<document-image>\n{DOCUMENT_IMAGE}\n</document-image>\n"
)

#: A Stage A prompt that asks for everything Stage A is supposed to withhold.
#: An operator can write this — the segmentation prompts are editable — so the
#: guarantee has to hold against it rather than against the shipped prompt,
#: which happens not to reference either placeholder and so makes the question
#: unobservable.
SEG_PROMPT_ASKING_FOR_VOCABULARY = (
    "Where does each document start?\n{BOUNDARY_HINTS}\n"
    "<document-types>\n{CLASS_NAMES_AND_DESCRIPTIONS}\n</document-types>\n"
    "<few-shot-examples>\n{FEW_SHOT_EXAMPLES}\n</few-shot-examples>\n"
    "<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n"
)

#: A single-pass page prompt that references ``{BOUNDARY_HINTS}``, which is a
#: two-stage-only placeholder. Nothing stops an operator writing it.
SINGLE_PASS_PROMPT_REFERENCING_HINTS = (
    "Classify this page.\n{BOUNDARY_HINTS}\n"
    "<document-types>\n{CLASS_NAMES_AND_DESCRIPTIONS}\n</document-types>\n"
    "<document-ocr-data>\n{DOCUMENT_TEXT}\n</document-ocr-data>\n"
)

#: Substrings that say a request carries class vocabulary. The description text
#: comes from ``_classes``, the example text from ``_classes(..., examples=True)``.
A_CLASS_DESCRIPTION = "distinctive layout"
A_FEW_SHOT_EXAMPLE = "EXAMPLE-PAGE-FOR"
#: The opening tag of the ``boundary_hints`` block, and the instruction inside
#: it that a class-assigning call must never be given.
HINTS_BLOCK_TAG = "<document-type-hints>"
HINTS_BLOCK_INSTRUCTION = "do NOT report a type"


def _png() -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
    return buffer.getvalue()


def _classes(*names: str, examples: bool = False) -> List[Dict[str, Any]]:
    schemas: List[Dict[str, Any]] = []
    for name in names:
        schema: Dict[str, Any] = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": name,
            "x-aws-idp-document-type": name,
            "type": "object",
            "description": f"A {name} document with distinctive layout",
            "properties": {},
        }
        if examples:
            # Text-only examples: no imagePath, so nothing reads S3 or the
            # local filesystem while the example content is built.
            schema["x-aws-idp-examples"] = [
                {
                    "name": f"{name}-example",
                    "x-aws-idp-class-prompt": f"{A_FEW_SHOT_EXAMPLE} {name}",
                }
            ]
        schemas.append(schema)
    return schemas


def _service(
    *,
    granularity: str = "page",
    strategy: str = "two_stage",
    classes: Optional[List[str]] = None,
    few_shot_examples: bool = False,
    segmentation: Optional[Dict[str, Any]] = None,
    labeling: Optional[Dict[str, Any]] = None,
    **classification: Any,
) -> ClassificationService:
    cfg: Dict[str, Any] = {
        "model": "us.amazon.nova-2-lite-v1:0",
        "system_prompt": "single pass system",
        "task_prompt": "single pass {CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
        "granularity": granularity,
        "strategy": strategy,
        "confidence": {"mode": "off"},
        "packet": {
            "system_prompt": "packet system",
            "task_prompt": "packet {CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}",
            "window_pages": "0",
        },
        "segmentation": {
            "system_prompt": "stage A system",
            "task_prompt": SEG_PAGE_PROMPT,
            "packet": {
                "system_prompt": "stage A packet system",
                "task_prompt": SEG_PACKET_PROMPT,
            },
            **(segmentation or {}),
        },
        "labeling": {
            "system_prompt": "stage B system",
            "task_prompt": LABEL_PROMPT,
            **(labeling or {}),
        },
    }
    cfg.update(classification)
    return ClassificationService(
        region="us-west-2",
        config={
            "classes": _classes(
                *(classes or ["invoice", "receipt"]), examples=few_shot_examples
            ),
            "classification": cfg,
        },
    )


def _document(page_count: int) -> Document:
    return Document(
        id="doc-1",
        input_key="doc.pdf",
        pages={
            str(i): Page(
                page_id=str(i),
                image_uri=f"s3://b/{i}.jpg",
                parsed_text_uri=f"s3://b/{i}.txt",
            )
            for i in range(1, page_count + 1)
        },
    )


def _reply(
    payload: Dict[str, Any], context: str, model: str = "nova"
) -> Dict[str, Any]:
    return {
        "response": {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}}
        },
        "metering": {f"{context}/bedrock/{model}": {"inputTokens": 10}},
    }


class _Router:
    """Answer each call according to the stage its config carries.

    The stage is on the config dict rather than inferred from the prompt,
    because that is exactly what the production code routes on — the metering
    context, the model and the prompts all come from it.
    """

    def __init__(
        self,
        *,
        boundaries: Optional[Dict[str, str]] = None,
        packet_segments: Optional[List[Dict[str, Any]]] = None,
        labels: Optional[List[str]] = None,
    ):
        self.boundaries = boundaries or {}
        self.packet_segments = packet_segments or []
        self.labels = list(labels or [])
        self.calls: List[tuple[str, List[Dict[str, Any]]]] = []

    def __call__(self, content, config):
        stage = config.get("stage")
        self.calls.append((stage, content))
        text = "".join(b["text"] for b in content if "text" in b)

        if stage == ClassificationService.STAGE_SEGMENTATION:
            if self.packet_segments:
                return _reply({"segments": self.packet_segments}, "Segmentation")
            # Page-granularity Stage A: find which page this call is about.
            for page_id, boundary in self.boundaries.items():
                if f"(document page {page_id})" in text or f"PAGE-{page_id}" in text:
                    return _reply({"document_boundary": boundary}, "Segmentation")
            return _reply({"document_boundary": "continue"}, "Segmentation")

        if stage == ClassificationService.STAGE_LABELING:
            label = self.labels.pop(0) if self.labels else "invoice"
            return _reply({"class": label}, "SectionClassification")

        return _reply(
            {"class": "invoice", "document_boundary": "start"}, "Classification"
        )


def _page_texts():
    """Text per page that the router can attribute back to a page id.

    Stage A at page granularity makes one call per page and the calls are
    indistinguishable from their stage alone, so the page's own text is what the
    router keys on.
    """

    def side_effect(uri):
        page_id = uri.rsplit("/", 1)[-1].split(".")[0]
        return f"PAGE-{page_id} content"

    return side_effect


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestStrategyConfig:
    def test_single_pass_is_the_default(self):
        from idp_common.config.models import IDPConfig

        assert IDPConfig().classification.strategy == "single_pass"

    def test_an_unknown_strategy_is_refused(self):
        with pytest.raises(Exception, match="strategy must be"):
            _service(strategy="three_stage")

    def test_an_unimplemented_labeling_method_is_refused_by_name(self):
        """'embeddings' and 'classifier' are the other two methods #1329
        describes; both need a measurement before their defaults can be set
        honestly, so the enum ships with one value and says so."""
        with pytest.raises(Exception, match="1329"):
            _service(labeling={"method": "embeddings"})

    def test_an_unknown_labeling_pages_value_is_refused(self):
        with pytest.raises(Exception, match="section_classification.pages must be"):
            _service(labeling={"pages": "last"})

    def test_an_unknown_boundary_hints_value_is_refused(self):
        with pytest.raises(Exception, match="boundary_hints must be"):
            _service(segmentation={"boundary_hints": "descriptions"})


@pytest.mark.unit
class TestStagePromptResolution:
    def test_stage_a_uses_the_page_segmentation_prompt_at_page_granularity(self):
        service = _service(granularity="page")
        config = service._get_classification_config(service.STAGE_SEGMENTATION)
        assert config["task_prompt"].startswith("Does a document start here?")
        assert config["system_prompt"] == "stage A system"
        assert config["metering_context"] == "Segmentation"

    def test_stage_a_uses_the_packet_segmentation_prompt_at_packet_granularity(self):
        service = _service(granularity="packet")
        config = service._get_classification_config(service.STAGE_SEGMENTATION)
        assert config["task_prompt"].startswith("Split this packet into ranges.")
        assert config["system_prompt"] == "stage A packet system"

    def test_stage_b_is_not_granularity_dependent(self):
        """Stage B is always one call about one segment's pages, whichever way
        the boundaries were found."""
        for granularity in ("page", "packet"):
            service = _service(granularity=granularity)
            config = service._get_classification_config(service.STAGE_LABELING)
            assert config["system_prompt"] == "stage B system"
            assert config["metering_context"] == "SectionClassification"

    def test_single_pass_keeps_the_classification_metering_context(self):
        """Unchanged units, so a cost comparison across this change is a
        straight read."""
        service = _service(strategy="single_pass")
        config = service._get_classification_config()
        assert config["metering_context"] == "Classification"

    def test_each_stage_can_name_its_own_model(self):
        service = _service(
            segmentation={"model": "us.anthropic.claude-sonnet-5"},
            labeling={"model": "us.amazon.nova-2-lite-v1:0"},
        )
        assert (
            service._get_classification_config(service.STAGE_SEGMENTATION)["model_id"]
            == "us.anthropic.claude-sonnet-5"
        )
        assert (
            service._get_classification_config(service.STAGE_LABELING)["model_id"]
            == "us.amazon.nova-2-lite-v1:0"
        )

    def test_an_unset_stage_model_falls_back_to_the_shared_one(self):
        service = _service()
        for stage in (service.STAGE_SEGMENTATION, service.STAGE_LABELING):
            assert (
                service._get_classification_config(stage)["model_id"]
                == "us.amazon.nova-2-lite-v1:0"
            )

    def test_stage_a_asks_for_no_confidence(self):
        """Stage A reports no class, so there is no class to be confident
        about, and output tokens spent here buy a number describing nothing."""
        service = _service(confidence={"mode": "topk", "task_prompt_topk": "BLOCK"})
        prompt = service._get_classification_config(service.STAGE_SEGMENTATION)[
            "task_prompt"
        ]
        assert "BLOCK" not in prompt

    def test_stage_b_uses_the_per_page_shaped_confidence_block(self):
        """Stage B returns one class, not a segments array, so the packet block
        would describe the wrong shape."""
        service = _service(
            granularity="packet",
            confidence={
                "mode": "topk",
                "task_prompt_topk": "PAGE BLOCK {TOP_K_CANDIDATES}",
                "task_prompt_topk_packet": "PACKET BLOCK",
            },
        )
        prompt = service._get_classification_config(service.STAGE_LABELING)[
            "task_prompt"
        ]
        assert "PAGE BLOCK 2" in prompt
        assert "PACKET BLOCK" not in prompt


@pytest.mark.unit
class TestBoundaryHints:
    def test_the_default_sends_no_class_vocabulary_at_all(self):
        """⚠️ The property with no symptom if it breaks: reintroducing the
        vocabulary leaves two-stage working and costing what single-pass cost."""
        service = _service()
        assert (
            service._format_boundary_hints(ClassificationService.STAGE_SEGMENTATION)
            == ""
        )

    def test_class_names_sends_names_only(self):
        service = _service(segmentation={"boundary_hints": "class_names"})
        hints = service._format_boundary_hints(ClassificationService.STAGE_SEGMENTATION)
        assert "invoice" in hints and "receipt" in hints
        # Not the descriptions, which are the bulk of the prefix.
        assert A_CLASS_DESCRIPTION not in hints

    @pytest.mark.parametrize(
        "stage",
        [
            ClassificationService.STAGE_CLASSIFICATION,
            ClassificationService.STAGE_LABELING,
        ],
    )
    def test_no_other_stage_can_render_the_hints_block(self, stage):
        """The block ends "do NOT report a type, only where each document starts
        and ends", which is the one thing a class-assigning call must do. So it
        is scoped to the stage, not to the setting being present."""
        service = _service(segmentation={"boundary_hints": "class_names"})
        assert service._format_boundary_hints(stage) == ""

    def test_the_stage_a_request_carries_no_class_descriptions(self):
        service = _service(granularity="packet")
        document = _document(2)
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 2}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.two_stage_classify_document(document)

        stage_a = [c for stage, c in router.calls if stage == "segmentation"]
        assert stage_a
        for content in stage_a:
            text = "".join(b["text"] for b in content if "text" in b)
            assert A_CLASS_DESCRIPTION not in text


# --------------------------------------------------------------------------
# The prefix saving is a property of the STAGE, not of the builder
# --------------------------------------------------------------------------


def _stage_a_text(service, document, router) -> str:
    """Every text block of every Stage A request this run makes, concatenated."""
    with (
        patch("idp_common.s3.get_text_content", side_effect=_page_texts()),
        patch("idp_common.image.prepare_image", return_value=_png()),
        patch.object(service, "_invoke_bedrock_model", side_effect=router),
    ):
        service.two_stage_classify_document(document)

    stage_a = [
        content
        for stage, content in router.calls
        if stage == ClassificationService.STAGE_SEGMENTATION
    ]
    assert stage_a, "no Stage A call was made, so this proves nothing"
    return "\n".join(b["text"] for content in stage_a for b in content if "text" in b)


@pytest.mark.unit
class TestStageAWithholdsTheVocabularyOnEveryPath:
    """Stage A had three content builders and only one withheld the vocabulary.

    ``_segment_pages`` passed an empty class listing (page granularity with no
    context pages); the context-page builder passed the full list and the packet
    builder the full markdown table, and all three spliced every configured
    few-shot example in at ``{FEW_SHOT_EXAMPLES}``. The shipped segmentation
    prompts reference neither placeholder, so the leak was invisible until an
    operator edited a prompt — and when it fired, two-stage still worked and
    simply cost what single-pass cost, which is the failure mode with no
    symptom. The guarantee now lives on the stage, so it holds at either
    granularity and any ``contextPagesCount``.
    """

    @pytest.mark.parametrize("granularity", ["page", "packet"])
    @pytest.mark.parametrize("context_pages", ["0", "1"])
    def test_stage_a_sends_no_descriptions_and_no_examples(
        self, granularity, context_pages
    ):
        service = _service(
            granularity=granularity,
            few_shot_examples=True,
            contextPagesCount=context_pages,
            segmentation={
                "task_prompt": SEG_PROMPT_ASKING_FOR_VOCABULARY,
                "packet": {
                    "system_prompt": "stage A packet system",
                    "task_prompt": SEG_PROMPT_ASKING_FOR_VOCABULARY,
                },
            },
        )
        router = _Router(
            packet_segments=(
                [{"ordinal_start_page": 1, "ordinal_end_page": 2}]
                if granularity == "packet"
                else None
            ),
            labels=["invoice", "receipt"],
        )
        text = _stage_a_text(service, _document(2), router)

        assert A_CLASS_DESCRIPTION not in text
        assert A_FEW_SHOT_EXAMPLE not in text

    def test_the_examples_really_are_configured(self):
        """The positive control. Without it the test above passes on a config
        that has no examples to leak, which is how a parametrised inertness
        test turns green by measuring nothing."""
        service = _service(granularity="packet", few_shot_examples=True)
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 2}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", side_effect=_page_texts()),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.two_stage_classify_document(_document(2))

        stage_b = [
            content
            for stage, content in router.calls
            if stage == ClassificationService.STAGE_LABELING
        ]
        assert stage_b
        text = "\n".join(
            b["text"] for content in stage_b for b in content if "text" in b
        )
        # Stage B is where the vocabulary and the examples belong, and the
        # shipped Stage B prompt references both placeholders.
        assert A_CLASS_DESCRIPTION in text
        assert A_FEW_SHOT_EXAMPLE in text

    def test_class_names_still_reach_stage_a_through_boundary_hints(self):
        """The supported way to give Stage A class *names* must keep working —
        withholding the vocabulary is about the two placeholders, not about the
        hints block."""
        service = _service(
            granularity="packet",
            few_shot_examples=True,
            segmentation={
                "boundary_hints": "class_names",
                "task_prompt": SEG_PROMPT_ASKING_FOR_VOCABULARY,
                "packet": {
                    "system_prompt": "stage A packet system",
                    "task_prompt": SEG_PROMPT_ASKING_FOR_VOCABULARY,
                },
            },
        )
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 2}],
            labels=["invoice"],
        )
        text = _stage_a_text(service, _document(2), router)

        assert HINTS_BLOCK_TAG in text
        assert "invoice" in text and "receipt" in text
        # Names only: still no descriptions and still no examples.
        assert A_CLASS_DESCRIPTION not in text
        assert A_FEW_SHOT_EXAMPLE not in text


@pytest.mark.unit
class TestBoundaryHintsDoNotLeakIntoSinglePass:
    """``boundary_hints`` lives in the ``two_stage`` subtree, which is inert
    under ``strategy: single_pass`` — but the subtree is readable either way, so
    the value reached the substitution dict regardless of the strategy. A
    single-pass page prompt referencing ``{BOUNDARY_HINTS}`` then received a
    block instructing the model *not* to report a type, which is the one thing
    that call exists to do.
    """

    def _run(self, service, document, router):
        with (
            patch("idp_common.s3.get_text_content", side_effect=_page_texts()),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            return service.classify_document(document)

    def test_a_single_pass_page_prompt_renders_no_hints_block(self):
        service = _service(
            strategy="single_pass",
            task_prompt=SINGLE_PASS_PROMPT_REFERENCING_HINTS,
            segmentation={"boundary_hints": "class_names"},
        )
        router = _Router()
        self._run(service, _document(2), router)

        calls = [
            content
            for stage, content in router.calls
            if stage == ClassificationService.STAGE_CLASSIFICATION
        ]
        assert calls
        text = "\n".join(b["text"] for content in calls for b in content if "text" in b)
        assert HINTS_BLOCK_TAG not in text
        assert HINTS_BLOCK_INSTRUCTION not in text
        # The class vocabulary a single-pass call legitimately needs is there,
        # so this is not passing because the prompt rendered empty.
        assert A_CLASS_DESCRIPTION in text

    def test_a_single_pass_packet_prompt_renders_no_hints_block(self):
        """The packet builder kept its own substitution dict, so it needs its
        own assertion; the stage it is given is the one its resolved config
        carries, which is how production reaches it."""
        service = _service(
            strategy="single_pass",
            granularity="packet",
            segmentation={"boundary_hints": "class_names"},
            packet={
                "system_prompt": "packet system",
                "task_prompt": SINGLE_PASS_PROMPT_REFERENCING_HINTS,
                "window_pages": "0",
            },
        )
        config = service._get_classification_config()
        content = service._build_packet_content(
            config["task_prompt"], "page 1 text", [], stage=config["stage"]
        )

        text = "\n".join(b["text"] for b in content if "text" in b)
        assert HINTS_BLOCK_TAG not in text
        assert HINTS_BLOCK_INSTRUCTION not in text
        assert A_CLASS_DESCRIPTION in text

    def test_stage_b_renders_no_hints_block_either(self):
        service = _service(
            labeling={"task_prompt": SINGLE_PASS_PROMPT_REFERENCING_HINTS},
            segmentation={"boundary_hints": "class_names"},
        )
        config = service._get_classification_config(
            ClassificationService.STAGE_LABELING
        )
        content = service._build_packet_content(
            config["task_prompt"], "page 1 text", [], stage=config["stage"]
        )

        text = "\n".join(b["text"] for b in content if "text" in b)
        assert HINTS_BLOCK_TAG not in text
        assert A_CLASS_DESCRIPTION in text


@pytest.mark.unit
class TestEveryContentBuilderDemandsItsStage:
    """⚠️ The one way this defect can come back without a symptom.

    A content builder that defaulted ``stage`` would hand the full vocabulary
    to any new call site that forgot to pass one: no type error, no failing
    test, and the only symptom a larger bill — which is the shape of the
    original defect. So ``stage`` is keyword-only with no default on every
    builder and on the substitution dict, and that is asserted from the
    signatures rather than from a sample call, so a builder added later is
    covered without this test being edited.
    """

    @staticmethod
    def _builders():
        import inspect

        for name, member in inspect.getmembers(
            ClassificationService, predicate=inspect.isfunction
        ):
            if not name.startswith("_build_"):
                continue
            parameter = inspect.signature(member).parameters.get("stage")
            if parameter is not None:
                yield name, parameter

    def test_there_are_builders_to_check(self):
        """Non-vacuity: a rename that moved every builder out of the
        ``_build_*`` namespace would otherwise leave this class green and
        checking nothing."""
        assert list(self._builders())

    def test_stage_is_keyword_only_and_required(self):
        import inspect

        offenders = {
            name: (parameter.kind.description, parameter.default)
            for name, parameter in self._builders()
            if parameter.kind is not inspect.Parameter.KEYWORD_ONLY
            or parameter.default is not inspect.Parameter.empty
        }
        assert not offenders, offenders

    def test_omitting_the_stage_raises_rather_than_rendering_the_vocabulary(self):
        service = _service(strategy="single_pass")
        with pytest.raises(TypeError, match="stage"):
            service._build_classification_substitutions("text", "classes")  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# Stage A at packet granularity
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTwoStagePacket:
    def _run(self, service, document, router):
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            return service.two_stage_classify_document(document)

    def test_one_segmentation_call_then_one_labeling_call_per_segment(self):
        service = _service(granularity="packet")
        router = _Router(
            packet_segments=[
                {"ordinal_start_page": 1, "ordinal_end_page": 2},
                {"ordinal_start_page": 3, "ordinal_end_page": 4},
            ],
            labels=["invoice", "receipt"],
        )
        result = self._run(service, _document(4), router)

        stages = [stage for stage, _ in router.calls]
        assert stages.count("segmentation") == 1
        assert stages.count("labeling") == 2
        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3", "4"]]
        assert [s.classification for s in result.sections] == ["invoice", "receipt"]

    def test_stage_a_segments_need_no_type_in_the_response(self):
        """Stage A is asked for ranges only; requiring a type would reject every
        segment it correctly returns."""
        service = _service(granularity="packet")
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 3}],
            labels=["invoice"],
        )
        result = self._run(service, _document(3), router)
        assert result.sections[0].page_ids == ["1", "2", "3"]

    def test_the_two_stages_are_metered_separately(self):
        service = _service(granularity="packet")
        router = _Router(
            packet_segments=[
                {"ordinal_start_page": 1, "ordinal_end_page": 1},
                {"ordinal_start_page": 2, "ordinal_end_page": 2},
            ],
            labels=["invoice", "receipt"],
        )
        result = self._run(service, _document(2), router)

        assert "Segmentation/bedrock/nova" in result.metering
        assert "SectionClassification/bedrock/nova" in result.metering
        assert "Classification/bedrock/nova" not in result.metering
        # One Stage A call, two Stage B calls.
        assert result.metering["Segmentation/bedrock/nova"]["inputTokens"] == 10
        assert (
            result.metering["SectionClassification/bedrock/nova"]["inputTokens"] == 20
        )

    def _router_for_two_segments(self) -> _Router:
        return _Router(
            packet_segments=[
                {"ordinal_start_page": 1, "ordinal_end_page": 1},
                {"ordinal_start_page": 2, "ordinal_end_page": 2},
            ],
            labels=["invoice", "receipt"],
        )

    def test_without_a_forced_class_every_segment_is_labeled(self):
        """The baseline the next test is measured against."""
        service = _service(granularity="packet")
        router = self._router_for_two_segments()
        self._run(service, _document(2), router)
        assert [stage for stage, _ in router.calls].count("labeling") == 2

    def test_a_forced_class_skips_stage_b_entirely(self):
        """The document's NAME asserts the class, so there is nothing to label —
        and a name-matched 40-page packet then costs its boundary calls and no
        labeling calls at all."""
        service = _service(granularity="packet")
        router = self._router_for_two_segments()

        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            result = service.two_stage_classify_document(
                _document(2), forced_class="receipt"
            )

        assert [stage for stage, _ in router.calls].count("labeling") == 0
        assert all(s.classification == "receipt" for s in result.sections)
        # The operator's assertion is not the model's, so it carries no score.
        assert all(s.confidence is None for s in result.sections)

    def test_stage_a_producing_nothing_fails_the_document(self):
        service = _service(granularity="packet")

        def boom(content, config):  # noqa: ARG001 - content unused, stage decides
            if config.get("stage") == "segmentation":
                raise RuntimeError("throttled")
            return _reply({"class": "invoice"}, "SectionClassification")

        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=boom),
        ):
            result = service.two_stage_classify_document(_document(2))
        assert any("Stage A" in e for e in result.errors)

    def test_a_segment_whose_labeling_fails_is_reported_not_invented(self):
        """Its boundaries are still good, and an invented class would propagate
        into extraction as though it had been decided."""
        service = _service(granularity="packet")

        def flaky(content, config):
            if config.get("stage") == "segmentation":
                return _reply(
                    {
                        "segments": [
                            {"ordinal_start_page": 1, "ordinal_end_page": 1},
                            {"ordinal_start_page": 2, "ordinal_end_page": 2},
                        ]
                    },
                    "Segmentation",
                )
            if "(document page 2)" in "".join(
                b["text"] for b in content if "text" in b
            ):
                raise RuntimeError("throttled")
            return _reply({"class": "invoice"}, "SectionClassification")

        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=flaky),
        ):
            result = service.two_stage_classify_document(_document(2))

        assert any("Stage B failed" in e for e in result.errors)
        assert result.sections[0].classification == "invoice"
        assert result.sections[1].classification is None


# --------------------------------------------------------------------------
# Stage A at page granularity
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestTwoStagePage:
    def _run(self, service, document, router):
        with (
            patch("idp_common.s3.get_text_content", side_effect=_page_texts()),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            return service.two_stage_classify_document(document)

    def test_one_boundary_call_per_page_then_one_label_call_per_segment(self):
        boundaries = {"1": "start", "2": "continue", "3": "start"}
        service = _service(granularity="page")
        router = _Router(boundaries=boundaries, labels=["invoice", "receipt"])
        result = self._run(service, _document(3), router)

        stages = [stage for stage, _ in router.calls]
        assert stages.count("segmentation") == 3
        # Two segments: pages 1-2 and page 3.
        assert stages.count("labeling") == 2
        assert [s.page_ids for s in result.sections] == [["1", "2"], ["3"]]

    def test_labeling_is_per_segment_not_per_page(self):
        """The saving: a long document is labeled once, not once per page."""
        boundaries = {str(i): ("start" if i == 1 else "continue") for i in range(1, 9)}
        service = _service(granularity="page")
        router = _Router(boundaries=boundaries, labels=["invoice"])
        result = self._run(service, _document(8), router)

        stages = [stage for stage, _ in router.calls]
        assert stages.count("segmentation") == 8
        assert stages.count("labeling") == 1
        assert result.sections[0].page_ids == [str(i) for i in range(1, 9)]

    def test_the_boundary_signal_is_persisted_on_the_page(self):
        boundaries = {"1": "start", "2": "continue"}
        service = _service(granularity="page")
        router = _Router(boundaries=boundaries, labels=["invoice"])
        result = self._run(service, _document(2), router)
        assert result.pages["1"].document_boundary == "start"
        assert result.pages["2"].document_boundary == "continue"

    def test_pages_carry_the_class_stage_b_decided(self):
        boundaries = {"1": "start", "2": "continue"}
        service = _service(granularity="page")
        router = _Router(boundaries=boundaries, labels=["receipt"])
        result = self._run(service, _document(2), router)
        assert all(p.classification == "receipt" for p in result.pages.values())


# --------------------------------------------------------------------------
# Which pages represent a segment
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestLabelingPages:
    def _segment(self, count: int):
        from idp_common.classification.models import Segment

        return Segment(page_ids=[str(i) for i in range(1, count + 1)])

    def test_first_sends_only_the_opening_page(self):
        service = _service(labeling={"pages": "first"})
        assert service._labeling_pages(self._segment(5)) == ["1"]

    def test_first_k_is_the_default_and_sends_k_pages(self):
        service = _service()
        assert service._labeling_pages(self._segment(5)) == ["1", "2"]

    def test_k_is_configurable(self):
        service = _service(labeling={"pages": "first_k", "k": "3"})
        assert service._labeling_pages(self._segment(5)) == ["1", "2", "3"]

    def test_all_sends_every_page(self):
        service = _service(labeling={"pages": "all"})
        assert service._labeling_pages(self._segment(4)) == ["1", "2", "3", "4"]

    def test_a_segment_shorter_than_k_sends_what_it_has(self):
        service = _service(labeling={"pages": "first_k", "k": "5"})
        assert service._labeling_pages(self._segment(2)) == ["1", "2"]


# --------------------------------------------------------------------------
# Class validation in Stage B
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestStageBClassValidation:
    """``enforceValidClasses`` applies here too: a class outside the vocabulary
    is exactly as wrong whether a page or a segment produced it."""

    def _run(self, service, replies):
        document = _document(2)

        calls = {"n": 0}

        def router(content, config):
            if config.get("stage") == "segmentation":
                return _reply(
                    {"segments": [{"ordinal_start_page": 1, "ordinal_end_page": 2}]},
                    "Segmentation",
                )
            index = min(calls["n"], len(replies) - 1)
            calls["n"] += 1
            return _reply({"class": replies[index]}, "SectionClassification")

        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            return service.two_stage_classify_document(document), calls["n"]

    def test_an_invalid_class_is_re_prompted(self):
        service = _service(
            granularity="packet", enforceValidClasses=True, maxValidationRetries=2
        )
        result, call_count = self._run(service, ["mystery", "invoice"])
        assert call_count == 2
        assert result.sections[0].classification == "invoice"

    def test_exhausted_retries_use_the_fallback_without_a_score(self):
        service = _service(
            granularity="packet",
            enforceValidClasses=True,
            maxValidationRetries=1,
            invalidClassFallback="unclassified",
        )
        result, call_count = self._run(service, ["mystery"])
        assert call_count == 2
        assert result.sections[0].classification == "unclassified"
        assert result.sections[0].confidence is None

    def test_validation_can_be_turned_off(self):
        service = _service(granularity="packet", enforceValidClasses=False)
        result, call_count = self._run(service, ["mystery"])
        assert call_count == 1
        assert result.sections[0].classification == "mystery"


# --------------------------------------------------------------------------
# Composition with the rest of the settings
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestComposition:
    def test_a_single_class_configuration_still_skips_both_stages(self):
        service = _service(classes=["invoice"], sectionSplitting="disabled")
        with patch.object(service, "_invoke_bedrock_model") as invoke:
            result = service.two_stage_classify_document(_document(3))
        invoke.assert_not_called()
        assert result.sections[0].classification == "invoice"

    def test_classify_document_dispatches_to_two_stage(self):
        service = _service(granularity="packet")
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 2}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.classify_document(_document(2))
        assert {stage for stage, _ in router.calls} == {"segmentation", "labeling"}

    def test_section_splitting_still_applies_to_the_labeled_segments(self):
        service = _service(granularity="packet", sectionSplitting="page")
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 3}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            result = service.two_stage_classify_document(_document(3))
        assert [s.page_ids for s in result.sections] == [["1"], ["2"], ["3"]]
        assert all(s.classification == "invoice" for s in result.sections)

    def test_modality_images_off_applies_to_both_stages(self):
        service = _service(granularity="packet", modality={"images": False})
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 2}],
            labels=["invoice"],
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image") as prepare,
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.two_stage_classify_document(_document(2))
        prepare.assert_not_called()
        for _, content in router.calls:
            assert not any("image" in block for block in content)

    def test_stage_a_windows_a_long_packet(self):
        service = _service(
            granularity="packet",
            packet={
                "system_prompt": "packet system",
                "task_prompt": "packet {DOCUMENT_TEXT}",
                "window_pages": "3",
                "overlap_pages": "1",
            },
        )
        router = _Router(
            packet_segments=[{"ordinal_start_page": 1, "ordinal_end_page": 3}],
            labels=["invoice"] * 10,
        )
        with (
            patch("idp_common.s3.get_text_content", return_value="text"),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            service.two_stage_classify_document(_document(7))
        # Windows over 7 pages at 3/1: [1-3], [3-5], [5-7].
        assert [stage for stage, _ in router.calls].count("segmentation") == 3

    def test_a_document_with_no_pages_fails_cleanly(self):
        service = _service()
        result = service.two_stage_classify_document(
            Document(id="d", input_key="d.pdf", pages={})
        )
        assert any("no pages" in e for e in result.errors)


@pytest.mark.unit
class TestAPreUpgradeProfileWithEmptyStagePrompts:
    """⚠️ Reported from a live stack: an existing profile switched to
    ``two_stage`` + ``packet`` failed EVERY document in ~28s with "Two-stage
    segmentation (Stage A) produced no segments", while a profile created after
    the upgrade worked end to end.

    The cause is not in this service. A profile stored in "full" format before
    ``classification.packet`` / ``.segmentation`` / ``.labeling`` existed was
    returned to the runtime directly, so those prompts arrived from Pydantic's
    field defaults — empty strings — rather than from the shipped defaults
    (2567, 1786/2676 and 1028 characters). ``ConfigurationManager`` now backfills
    them, and ``test_configuration_manager_coverage.py`` holds that.

    What is pinned HERE is the second half: an empty stage prompt must resolve to
    the SHIPPED default rather than to the single-pass page prompt. The old
    fallback could not work — the page prompt makes the model answer in page
    shape, which the segments parser cannot read — so it turned a missing field
    into a total failure. **A fallback that always fails is not a fallback.**
    """

    def _service(self, classification_overrides):
        base = {
            "model": "us.amazon.nova-2-lite-v1:0",
            "granularity": "packet",
            "strategy": "two_stage",
            "task_prompt": (
                "PAGE PROMPT: classify this page and give a document_boundary. "
                "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}"
            ),
            "system_prompt": "PAGE SYSTEM PROMPT",
            "confidence": {"mode": "off"},
        }
        base.update(classification_overrides)
        return ClassificationService(
            region="us-west-2",
            config={
                "classes": [
                    {
                        "$id": "invoice",
                        "x-aws-idp-document-type": "invoice",
                        "type": "object",
                        "description": "An invoice",
                        "properties": {},
                    }
                ],
                "classification": base,
            },
        )

    def test_an_empty_packet_stage_a_prompt_uses_the_shipped_one(self, caplog):
        """The exact reported shape: packet + two_stage with every new prompt
        empty."""
        import logging

        service = self._service(
            {
                "packet": {"task_prompt": "", "system_prompt": ""},
                "segmentation": {"packet": {"task_prompt": "", "system_prompt": ""}},
                "labeling": {"task_prompt": "", "system_prompt": ""},
            }
        )
        with caplog.at_level(logging.WARNING):
            prompt = service._stage_prompt(
                ClassificationService.STAGE_SEGMENTATION, "task_prompt"
            )
        assert prompt, "Stage A resolved to an empty prompt"
        assert "PAGE PROMPT" not in prompt, (
            "fell back to the page prompt, which the segments parser cannot read"
        )
        # It is the shipped prompt, and it asks for what the parser reads.
        assert "segments" in prompt
        assert "SHIPPED default" in caplog.text

    def test_an_empty_labeling_prompt_uses_the_shipped_one(self):
        service = self._service({"labeling": {"task_prompt": ""}})
        prompt = service._stage_prompt(
            ClassificationService.STAGE_LABELING, "task_prompt"
        )
        assert prompt
        assert "PAGE PROMPT" not in prompt

    def test_an_empty_packet_prompt_uses_the_shipped_one_in_single_pass(self):
        """Same class of failure off the two-stage path: `packet` granularity
        with an empty packet prompt used to send the page prompt."""
        service = self._service(
            {"strategy": "single_pass", "packet": {"task_prompt": ""}}
        )
        prompt = service._stage_prompt(
            ClassificationService.STAGE_CLASSIFICATION, "task_prompt"
        )
        assert "PAGE PROMPT" not in prompt
        assert "segments" in prompt

    def test_an_operators_own_prompt_still_wins(self):
        """The shipped default is a fallback for ABSENT, never a preference. A
        configured prompt must not be replaced by ours — that is the hazard the
        0.8 migration's prompt relocation exists to prevent."""
        mine = "MY OWN STAGE A: return segments with ordinal_start_page"
        service = self._service({"segmentation": {"packet": {"task_prompt": mine}}})
        assert (
            service._stage_prompt(
                ClassificationService.STAGE_SEGMENTATION, "task_prompt"
            )
            == mine
        )

    def test_the_shipped_defaults_are_read_from_disk_not_hardcoded(self):
        """So the fallback is what THIS build would write into a new profile."""
        shipped = ClassificationService._shipped_classification_defaults()
        assert shipped, "the shipped classification defaults did not load"
        assert shipped["one_stage"]["packet"]["task_prompt"].strip()
        assert shipped["two_stage"]["segmentation"]["llm"]["packet"][
            "task_prompt"
        ].strip()
        assert shipped["two_stage"]["section_classification"]["llm"][
            "task_prompt"
        ].strip()

    def test_an_unreadable_defaults_file_does_not_raise(self, monkeypatch):
        """A missing defaults file must not turn into a classification failure;
        the caller's existing fallback still applies."""
        ClassificationService._shipped_classification_defaults.cache_clear()
        monkeypatch.setattr(
            "idp_common.config.merge_utils.load_system_defaults",
            lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("gone")),
        )
        try:
            assert ClassificationService._shipped_classification_defaults() == {}
        finally:
            ClassificationService._shipped_classification_defaults.cache_clear()


# --------------------------------------------------------------------------
# A declared file shape fixes the segments, so segmentation is skipped
# --------------------------------------------------------------------------


@pytest.mark.unit
class TestADeclaredFileShapeSkipsSegmentation:
    """Under "one document" or "one per page" the segments are known before any
    call, and section forming would discard whatever segmentation returned. At
    packet granularity with images that is the expensive call, so it is not made.
    """

    def _run(self, service, document, router):
        with (
            patch("idp_common.s3.get_text_content", side_effect=_page_texts()),
            patch("idp_common.image.prepare_image", return_value=_png()),
            patch.object(service, "_invoke_bedrock_model", side_effect=router),
        ):
            return service.two_stage_classify_document(document)

    @staticmethod
    def _stages(router):
        return [stage for stage, _ in router.calls]

    @pytest.mark.parametrize("granularity", ["page", "packet"])
    def test_one_document_makes_one_section_classification_call(self, granularity):
        service = _service(
            granularity=granularity,
            sectionSplitting="disabled",
            labeling={"pages": "all"},
        )
        router = _Router(labels=["receipt"])
        result = self._run(service, _document(4), router)

        assert self._stages(router) == [ClassificationService.STAGE_LABELING]
        assert [s.page_ids for s in result.sections] == [["1", "2", "3", "4"]]
        assert result.sections[0].classification == "receipt"
        assert not any(k.startswith("Segmentation/") for k in result.metering)
        # The one call saw every page, because `pages: all` asked for it.
        text = "".join(b["text"] for b in router.calls[0][1] if "text" in b)
        assert all(f"PAGE-{i}" in text for i in range(1, 5))

    def test_one_per_page_classifies_each_page_once(self):
        service = _service(sectionSplitting="page")
        router = _Router(labels=["invoice", "receipt", "invoice"])
        result = self._run(service, _document(3), router)

        assert self._stages(router) == [ClassificationService.STAGE_LABELING] * 3
        assert [s.page_ids for s in result.sections] == [["1"], ["2"], ["3"]]

    def test_detected_boundaries_still_run_segmentation(self):
        """The default is untouched: there, boundaries are what segmentation is for."""
        service = _service(sectionSplitting="llm_determined")
        router = _Router(boundaries={"1": "start", "2": "continue"}, labels=["invoice"])
        self._run(service, _document(2), router)

        assert self._stages(router).count(ClassificationService.STAGE_SEGMENTATION) == 2
