# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Config migration v0.7 -> v0.8: ``classificationMethod`` becomes ``granularity``.

The transform itself is a two-value rename. **The behaviour preservation is the
hard part**, and it has two halves that fail in opposite directions:

⚠️ **Modality must NOT be written.** An unset flag is inferred from the prompt's
placeholders, which is exactly what the service did before the setting existed —
including for the documented workaround of deleting ``{DOCUMENT_IMAGE}`` to get
text-only classification. Writing ``images: true`` to match
``multimodalPageLevelClassification``'s *name* would start sending images to a
prompt edited not to ask for them: a cost and behaviour change, on the configs
of the users who followed the documentation.

⚠️ **A holistic config's flat prompts must move into ``classification.packet``.**
``task_prompt`` was one key shared by both methods and no packet-shaped default
has ever shipped, so the only way holistic ever worked was a hand-written,
packet-shaped flat ``task_prompt``. Setting ``granularity: packet`` alone makes
the service prefer ``packet.task_prompt`` — the shipped default — silently
replacing the operator's own prompt with ours, on the one path where the output
shape is what the parser depends on.

The call-site risk is the same one ``test_merge_migration_order.py`` pins for the
v0.5 hop: a sparse override delta carrying the legacy key must survive being
deep-merged onto the already-new-shaped defaults.
"""

from __future__ import annotations

import json

import pytest

from idp_common.config.merge_utils import merge_config_with_defaults
from idp_common.config.migrations import migrate_config
from idp_common.config.migrations.v07_to_v08 import (
    TARGET_VERSION,
    migrate_v07_to_v08,
)
from idp_common.config.models import CONFIG_FORMAT_VERSION, IDPConfig

# A realistic hand-written packet prompt. It MUST name the keys the parser reads
# — `segments` with `ordinal_start_page` — or every segment it returns is
# skipped, which is why requiring the page ordinal alongside the word "segments"
# cannot misclassify a prompt that actually worked under holistic.
PACKET_PROMPT = (
    "Return segments with ordinal_start_page and ordinal_end_page.\n"
    "{CLASS_NAMES_AND_DESCRIPTIONS}\n{DOCUMENT_TEXT}\n"
)
PAGE_PROMPT = "Classify this page.\n{DOCUMENT_TEXT}\n{DOCUMENT_IMAGE}\n"


@pytest.mark.unit
class TestGranularityMapping:
    def test_the_page_method_maps_to_page(self):
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "multimodalPageLevelClassification"
                }
            }
        )
        assert out["classification"]["granularity"] == "page"
        assert "classificationMethod" not in out["classification"]

    def test_the_holistic_method_maps_to_packet(self):
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification"
                }
            }
        )
        assert out["classification"]["granularity"] == "packet"
        assert "classificationMethod" not in out["classification"]

    @pytest.mark.parametrize(
        "spelling",
        [
            "textbasedHolisticClassification",
            "textbasedholisticclassification",
            "  TextBasedHolisticClassification  ",
        ],
    )
    def test_the_value_is_matched_case_and_space_insensitively(self, spelling):
        """A hand-written config is where this key most often came from."""
        out = migrate_v07_to_v08({"classification": {"classificationMethod": spelling}})
        assert out["classification"]["granularity"] == "packet"

    def test_an_unrecognised_value_maps_to_page_with_a_warning(self, caplog):
        """Same treatment the service gave it: an unknown method took the
        page-level path rather than failing."""
        import logging

        with caplog.at_level(logging.WARNING):
            out = migrate_v07_to_v08(
                {"classification": {"classificationMethod": "nonsense"}}
            )
        assert out["classification"]["granularity"] == "page"
        assert "Unrecognised" in caplog.text

    def test_an_explicit_granularity_wins_over_the_legacy_key(self):
        """Re-running over a hybrid config must not clobber a deliberate
        setting."""
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "granularity": "page",
                }
            }
        )
        assert out["classification"]["granularity"] == "page"

    def test_the_empty_placeholder_is_not_treated_as_explicit(self):
        """The pre-0.8 default wrote `granularity: ""` meaning "derive from the
        method", so an empty value must not block the derivation."""
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "granularity": "",
                }
            }
        )
        assert out["classification"]["granularity"] == "packet"


@pytest.mark.unit
class TestModalityIsLeftAlone:
    """⚠️ The half that fails by doing too much."""

    def test_no_modality_is_written_for_the_page_method(self):
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "multimodalPageLevelClassification",
                    "task_prompt": PAGE_PROMPT,
                }
            }
        )
        assert "modality" not in out["classification"]

    def test_a_text_only_workaround_config_keeps_sending_text_only(self):
        """The documented pre-setting way to get text-only classification was to
        delete {DOCUMENT_IMAGE}. Writing `images: true` to match the method's
        name would start sending images to that prompt."""
        text_only = "Classify this page.\n{DOCUMENT_TEXT}\n"
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "multimodalPageLevelClassification",
                    "task_prompt": text_only,
                }
            }
        )
        assert "modality" not in out["classification"]
        # And the service's inference over that prompt still says text-only.
        merged = merge_config_with_defaults(out, validate=False)
        merged["classification"]["task_prompt"] = text_only
        config = IDPConfig(**merged)
        assert config.classification.combined.llm.modality.images is None

    def test_an_explicit_modality_survives(self):
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "multimodalPageLevelClassification",
                    "modality": {"text": True, "images": False},
                }
            }
        )
        assert out["classification"]["modality"] == {"text": True, "images": False}


@pytest.mark.unit
class TestHolisticPromptRelocation:
    """⚠️ The half that fails by doing too little."""

    def test_the_flat_prompts_move_into_packet(self):
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "system_prompt": "You split packets.",
                    "task_prompt": PACKET_PROMPT,
                }
            }
        )
        packet = out["classification"]["packet"]
        assert packet["task_prompt"] == PACKET_PROMPT
        assert packet["system_prompt"] == "You split packets."

    def test_the_flat_prompts_are_copied_not_cleared(self):
        """Split-then-classify section classification falls back to them, and a
        config later switched back to `page` would otherwise have no page prompt at all."""
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "task_prompt": PACKET_PROMPT,
                }
            }
        )
        assert out["classification"]["task_prompt"] == PACKET_PROMPT

    def test_the_hand_written_prompt_overwrites_a_shipped_packet_default(self):
        """⚠️ The relocation must NOT be guarded on "a packet prompt is already
        set". The migration runs on the MERGED config and the shipped defaults
        always supply `classification.packet.task_prompt`, so such a guard fires
        every single time and discards the operator's own prompt — which is
        exactly what the relocation exists to prevent. The guard that decides is
        the prompt's shape, not its presence."""
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "task_prompt": "my own: return segments with ordinal_start_page",
                    "packet": {
                        "task_prompt": "the shipped default, segments ordinal_start_page"
                    },
                }
            }
        )
        assert (
            out["classification"]["packet"]["task_prompt"]
            == "my own: return segments with ordinal_start_page"
        )

    def test_a_flat_page_prompt_is_not_relocated(self):
        """A legacy holistic config whose flat prompt was never customised was
        BROKEN, not working: the shipped flat default is the PAGE prompt, which
        the segments parser cannot read. Carrying it forward would preserve the
        breakage; leaving it behind gets the config the shipped packet prompt,
        which works."""
        page_shaped = (
            'Classify this page. Respond {"class": ..., "document_boundary": ...}'
        )
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "task_prompt": page_shaped,
                    "packet": {
                        "task_prompt": "the shipped packet one, segments ordinal_start_page"
                    },
                }
            }
        )
        assert out["classification"]["granularity"] == "packet"
        assert (
            out["classification"]["packet"]["task_prompt"]
            == "the shipped packet one, segments ordinal_start_page"
        )
        assert out["classification"]["task_prompt"] == page_shaped

    def test_the_system_prompt_moves_with_the_task_prompt_or_not_at_all(self):
        """Half of a hand-written pair is worse than neither: the two were
        written to describe the same output shape."""
        page_shaped = "Classify this page."
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "system_prompt": "a page system prompt",
                    "task_prompt": page_shaped,
                }
            }
        )
        assert "packet" not in out["classification"]

    def test_a_page_config_has_its_prompts_left_where_they_are(self):
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "multimodalPageLevelClassification",
                    "task_prompt": PAGE_PROMPT,
                }
            }
        )
        assert out["classification"]["task_prompt"] == PAGE_PROMPT
        assert "packet" not in out["classification"]

    def test_a_blank_flat_prompt_is_not_relocated(self):
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "task_prompt": "   ",
                }
            }
        )
        assert "packet" not in out["classification"]

    def test_the_merged_config_path_keeps_the_hand_written_prompt(self):
        """The shape a real deployment has: a sparse custom delta deep-merged
        onto the full shipped defaults, so `packet.task_prompt` is present from
        the defaults before the migration ever runs."""
        hand_written = "MY OWN: return segments with ordinal_start_page"
        merged = merge_config_with_defaults(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "task_prompt": hand_written,
                }
            },
            validate=False,
        )
        config = IDPConfig(**merged)
        assert config.classification.combined.granularity == "packet"
        assert config.classification.combined.packet.task_prompt == hand_written

    def test_the_relocated_prompt_is_what_the_service_resolves(self):
        """End to end: a legacy holistic config must still run ITS OWN prompt.
        This is the assertion the whole relocation exists for.

        ⚠️ **Merged with the defaults, which is what makes it an assertion.** On
        an unmerged config `ClassificationPacketConfig.task_prompt` is `""` and
        `_stage_prompt` falls back to the flat prompt, so this passes with the
        relocation deleted. A real deployment is always merged, and there the
        shipped packet prompt is present and wins."""
        from idp_common.classification.service import ClassificationService

        legacy = {
            "classes": [
                {
                    "$id": "invoice",
                    "x-aws-idp-document-type": "invoice",
                    "type": "object",
                    "description": "An invoice",
                    "properties": {},
                }
            ],
            "classification": {
                "model": "us.amazon.nova-2-lite-v1:0",
                "classificationMethod": "textbasedHolisticClassification",
                "system_prompt": "You split packets.",
                "task_prompt": PACKET_PROMPT,
                "confidence": {"mode": "off"},
            },
        }
        service = ClassificationService(
            region="us-west-2",
            config=merge_config_with_defaults(legacy, validate=False),
        )
        assert service.granularity == "packet"
        resolved = service._get_classification_config()
        assert resolved["task_prompt"] == PACKET_PROMPT
        assert resolved["system_prompt"] == "You split packets."


@pytest.mark.unit
class TestIdempotenceAndStamping:
    def test_running_twice_changes_nothing(self):
        once = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "task_prompt": PACKET_PROMPT,
                }
            }
        )
        twice = migrate_v07_to_v08(once)
        assert twice == once

    def test_a_clean_config_is_returned_unchanged_by_identity(self):
        clean = {
            "config_format_version": TARGET_VERSION,
            "classification": {"granularity": "packet"},
        }
        assert migrate_v07_to_v08(clean) is clean

    def test_the_stamp_is_set(self):
        out = migrate_v07_to_v08({"classification": {"granularity": "page"}})
        assert out["config_format_version"] == TARGET_VERSION

    def test_a_newer_stamp_is_not_downgraded(self):
        """A rollback: an older release reading a config a newer one wrote.
        Rewriting the stamp down would erase the record that this config came
        from a newer format, so a later roll-forward would skip the migration it
        needs."""
        out = migrate_v07_to_v08(
            {
                "config_format_version": "0.9",
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification"
                },
            }
        )
        assert out["config_format_version"] == "0.9"
        assert out["classification"]["granularity"] == "packet"

    def test_the_target_matches_the_models_current_version(self):
        # This hop's target is fixed; the chain carries on to the current format.
        assert TARGET_VERSION == "0.8"
        assert migrate_config({})["config_format_version"] == CONFIG_FORMAT_VERSION

    def test_a_non_dict_is_returned_unchanged(self):
        assert migrate_v07_to_v08(None) is None
        assert migrate_v07_to_v08("nope") == "nope"

    def test_a_config_with_no_classification_block_is_untouched(self):
        out = migrate_v07_to_v08({"ocr": {"backend": "textract"}})
        assert out["ocr"] == {"backend": "textract"}


@pytest.mark.unit
class TestTheWholeChainAndTheMergePath:
    def test_the_chain_applies_this_step(self):
        out = migrate_config(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification"
                }
            }
        )
        assert out["classification"]["combined"]["granularity"] == "packet"
        assert "classificationMethod" not in json.dumps(out)

    def test_a_v05_config_reaches_v08(self):
        """A stored config from three formats ago needs every hop."""
        out = migrate_config(
            {
                "config_format_version": "0.5",
                "classification": {
                    "classificationMethod": "multimodalPageLevelClassification"
                },
            }
        )
        assert out["config_format_version"] == CONFIG_FORMAT_VERSION
        assert out["classification"]["combined"]["granularity"] == "page"

    def test_a_sparse_override_delta_survives_the_merge(self):
        """⚠️ The call-site risk, and the P0 shape #1079's sibling test pins for
        the v0.5 hop: the defaults are already new-shaped, so a legacy-shaped
        delta merged onto them must still be migrated rather than dropped."""
        delta = {
            "classification": {
                "classificationMethod": "textbasedHolisticClassification",
                "task_prompt": PACKET_PROMPT,
            }
        }
        merged = merge_config_with_defaults(delta, validate=False)
        config = IDPConfig(**merged)
        assert config.classification.combined.granularity == "packet"
        assert config.classification.combined.packet.task_prompt == PACKET_PROMPT

    def test_the_model_drops_the_legacy_key(self):
        """It is no longer a field, and `extra="ignore"` means a config still
        carrying it past the migration loses it quietly — which is why the
        migration runs in the model's own before-validator."""
        config = IDPConfig(
            **{
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification"
                }
            }
        )
        assert not hasattr(config.classification, "classificationMethod")
        assert config.classification.combined.granularity == "packet"

    def test_the_shipped_defaults_carry_no_legacy_key(self):
        """Item 4 of the tidy-up: the default config uses the new settings."""
        import pathlib

        import yaml

        defaults = (
            pathlib.Path(__file__).resolve().parents[3]
            / "idp_common"
            / "config"
            / "system_defaults"
            / "base-classification.yaml"
        )
        raw = yaml.safe_load(defaults.read_text())["classification"]
        assert "classificationMethod" not in raw
        assert raw["combined"]["granularity"] == "page"

    def test_an_explicit_packet_granularity_blocks_the_relocation(self):
        """⚠️ The mirror image of the relocation's own purpose. A config that
        already said `granularity: packet` has had `classification.packet`
        available all along, so its `packet.task_prompt` may be a deliberate
        setting; overwriting it from the flat key would be this function causing
        the loss it exists to prevent. Only a granularity DERIVED in this call
        relocates."""
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "granularity": "packet",
                    "task_prompt": "stale flat: return segments with ordinal_start_page",
                    "packet": {
                        "task_prompt": "deliberate: return segments with ordinal_start_page"
                    },
                }
            }
        )
        assert out["classification"]["granularity"] == "packet"
        assert (
            out["classification"]["packet"]["task_prompt"]
            == "deliberate: return segments with ordinal_start_page"
        )

    def test_the_empty_placeholder_still_relocates(self):
        """Empty is not explicit, so it derives — and deriving relocates."""
        out = migrate_v07_to_v08(
            {
                "classification": {
                    "classificationMethod": "textbasedHolisticClassification",
                    "granularity": "",
                    "task_prompt": PACKET_PROMPT,
                }
            }
        )
        assert out["classification"]["packet"]["task_prompt"] == PACKET_PROMPT
