# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Tests for configuration Pydantic models.
"""

import logging

import pytest

from idp_common.config.models import (
    AgenticConfig,
    ChatConfig,
    ExtractionConfig,
    IDPConfig,
)


class TestConfigModels:
    """Test configuration Pydantic models"""

    def test_agentic_config_with_booleans(self):
        """Test AgenticConfig with boolean values"""
        config_dict = {"enabled": False, "review_agent": True}
        config = AgenticConfig.model_validate(config_dict)

        assert config.enabled is False
        assert config.review_agent is True
        assert isinstance(config.enabled, bool)
        assert isinstance(config.review_agent, bool)

    def test_agentic_config_with_string_booleans(self):
        """Test AgenticConfig with string boolean values (legacy)"""
        config_dict = {"enabled": "false", "review_agent": "true"}
        config = AgenticConfig.model_validate(config_dict)

        # Pydantic should convert string booleans
        assert config.enabled is False
        assert config.review_agent is True

    def test_extraction_mode_derives_agentic_enabled(self):
        """extraction.mode is authoritative and derives agentic.enabled."""
        from idp_common.config.models import ExtractionConfig

        # mode=advanced -> agentic on
        c = ExtractionConfig(mode="advanced")
        assert c.agentic.enabled is True
        # mode=simple -> agentic off (mode wins even over an explicit agentic.enabled)
        c = ExtractionConfig(mode="simple", agentic={"enabled": True})
        assert c.agentic.enabled is False
        # legacy config with no mode -> inferred from agentic.enabled
        c = ExtractionConfig(agentic={"enabled": True})
        assert c.mode == "advanced"
        c = ExtractionConfig(agentic={"enabled": False})
        assert c.mode == "simple"

    def test_extraction_mode_rejects_unknown(self):
        from idp_common.config.models import ExtractionConfig

        with pytest.raises(Exception):
            ExtractionConfig(mode="turbo")

    def test_extraction_config_with_string_numbers(self):
        """Test ExtractionConfig with string numeric values"""
        config_dict = {
            "model": "us.amazon.nova-pro-v1:0",
            "temperature": "0.5",
            "top_p": "0.1",
            "top_k": "5",
            # max_tokens is no longer an ExtractionConfig field (extra="ignore"):
            # output is always requested at the model maximum.
            "max_tokens": "10000",
            "agentic": {"enabled": False, "review_agent": False},
        }
        config = ExtractionConfig.model_validate(config_dict)

        # Validators should convert strings to numbers
        assert config.temperature == 0.5
        assert config.top_p == 0.1
        assert config.top_k == 5.0
        assert not hasattr(config, "max_tokens")

        # Types should be correct
        assert isinstance(config.temperature, float)
        assert isinstance(config.top_p, float)
        assert isinstance(config.top_k, float)

    def test_extraction_config_with_native_numbers(self):
        """Test ExtractionConfig with native numeric values"""
        config_dict = {
            "model": "us.amazon.nova-pro-v1:0",
            "temperature": 0.5,
            "top_p": 0.1,
            "top_k": 5.0,
            "agentic": {"enabled": False, "review_agent": False},
        }
        config = ExtractionConfig.model_validate(config_dict)

        assert config.temperature == 0.5
        assert config.top_p == 0.1
        assert config.top_k == 5.0
        assert not hasattr(config, "max_tokens")

    def test_full_config_with_mixed_types(self):
        """Test full IDPConfig with mixed type representations"""
        config_dict = {
            "ocr": {
                "backend": "textract",
                "features": [{"name": "LAYOUT"}, {"name": "TABLES"}],
            },
            "classification": {
                "model": "us.amazon.nova-pro-v1:0",
                "temperature": "0.0",
                "top_p": "0.1",
                "top_k": "5",
                "max_tokens": "4096",
            },
            "extraction": {
                "model": "us.amazon.nova-pro-v1:0",
                "temperature": 0.0,
                "top_p": 0.1,
                "top_k": 5,
                "agentic": {"enabled": False, "review_agent": True},
            },
            "assessment": {
                "model": "us.amazon.nova-lite-v1:0",
                "enabled": True,
                "temperature": "0.0",
            },
            "classes": [],
        }

        config = IDPConfig.model_validate(config_dict)

        # Booleans
        assert config.extraction.agentic.enabled is False
        assert config.extraction.agentic.review_agent is True
        # v0.6: assessment.enabled migrated to extraction.confidence.enabled
        assert config.extraction.confidence.enabled is True

        # Numbers from strings
        assert config.classification.one_stage.llm.temperature == 0.0
        assert config.classification.one_stage.llm.max_tokens == 4096

        # Numbers from natives
        assert config.extraction.top_p == 0.1
        assert not hasattr(config.extraction, "max_tokens")

    def test_classification_valid_class_enforcement_defaults(self):
        """New class-enforcement fields default to enabled with sane values."""
        from idp_common.config.models import ClassificationConfig

        cfg = ClassificationConfig()
        validation = cfg.one_stage.llm.validation
        assert validation.enforceValidClasses is True
        assert validation.maxValidationRetries == 2
        assert cfg.fallback_class == "unclassified"

    def test_classification_valid_class_enforcement_parsing(self):
        """String-typed stored config values parse into the correct types."""
        from idp_common.config.models import ClassificationConfig

        cfg = ClassificationConfig.model_validate(
            {
                "enforceValidClasses": "false",
                "maxValidationRetries": "3",
                "invalidClassFallback": "other",
            }
        )
        # Flat (pre-0.9) keys passed straight to the model are migrated, not
        # dropped: direct construction is a second entry point to protect.
        for validation in (
            cfg.one_stage.llm.validation,
            cfg.two_stage.section_classification.llm.validation,
        ):
            assert validation.enforceValidClasses is False
            assert validation.maxValidationRetries == 3
        assert cfg.fallback_class == "other"

        # Negative retries are clamped to 0.
        clamped = ClassificationConfig.model_validate({"maxValidationRetries": "-1"})
        assert clamped.one_stage.llm.validation.maxValidationRetries == 0

    def test_config_type_hints(self):
        """Test that config can be used as type hint"""

        def process_config(config: ExtractionConfig) -> bool:
            """Example function with type hint"""
            if config.agentic.enabled:
                return True
            return False

        config_dict = {
            "model": "us.amazon.nova-pro-v1:0",
            "agentic": {"enabled": True},
        }
        config = ExtractionConfig.model_validate(config_dict)

        # This should work with type hints
        result = process_config(config)
        assert result is True

    def test_confidence_list_batch_size_config(self):
        """Confidence list batching config (v0.6: extraction.confidence). Any
        leftover retired ``granular.*`` keys are ignored, not errors."""
        from idp_common.config import ConfidenceConfig

        config_dict = {
            "model": "us.amazon.nova-lite-v1:0",
            "list_batch_size": "5",
            # Retired granular sub-config: tolerated (ignored) for back-compat.
            "granular": {"enabled": True, "max_workers": "20"},
        }
        config = ConfidenceConfig.model_validate(config_dict)

        assert config.list_batch_size == 5
        assert not hasattr(config, "granular")

    def test_required_int_none_falls_back_to_default(self):
        """A stored config may carry an explicit ``null`` / empty string for a
        required int field (e.g. ``list_batch_size``, ``max_empty_line_gap``).
        These must fall back to the field default rather than crash with
        ``int(None)`` when the config is re-validated (e.g. on upgrade)."""
        from idp_common.config import ConfidenceConfig
        from idp_common.config.models import TableParsingConfig

        # Explicit None -> default (25)
        assert (
            ConfidenceConfig.model_validate({"list_batch_size": None}).list_batch_size
            == 25
        )
        # Empty string -> default
        assert (
            ConfidenceConfig.model_validate({"list_batch_size": ""}).list_batch_size
            == 25
        )
        # Valid values still coerce
        assert (
            ConfidenceConfig.model_validate({"list_batch_size": "8"}).list_batch_size
            == 8
        )

        # Same guard on a different non-optional int field (default 3)
        assert (
            TableParsingConfig.model_validate(
                {"max_empty_line_gap": None}
            ).max_empty_line_gap
            == 3
        )
        assert (
            TableParsingConfig.model_validate(
                {"max_empty_line_gap": 5}
            ).max_empty_line_gap
            == 5
        )

    def test_config_validation_range_checks(self):
        """Test that validation enforces ranges"""
        # temperature must be between 0 and 1
        with pytest.raises(Exception):  # Pydantic ValidationError
            ExtractionConfig.model_validate(
                {
                    "model": "test",
                    "temperature": 2.0,  # Invalid: > 1.0
                }
            )

    def test_config_defaults(self):
        """Test that defaults are applied correctly"""
        config_dict = {
            "model": "us.amazon.nova-pro-v1:0",
            # No agentic config provided
        }
        config = ExtractionConfig.model_validate(config_dict)

        # Should have defaults
        assert config.agentic.enabled is False
        assert config.agentic.review_agent is False
        assert config.temperature == 0.0
        assert not hasattr(config, "max_tokens")


class TestChatConfig:
    """Tests for the Chat-with-Document configuration section."""

    def test_chat_config_defaults(self):
        """ChatConfig populates sensible defaults when no overrides are given."""
        cfg = ChatConfig.model_validate({})

        assert cfg.enabled is True
        # Default should be a large-context Opus model (see decision in CHANGELOG).
        assert cfg.model == "us.anthropic.claude-opus-4-8:1m"
        assert cfg.temperature == 0.0
        # max_tokens defaults to None (unset) => Bedrock client resolves the
        # model's maximum output limit. An empty string also maps to None.
        assert cfg.max_tokens is None
        assert ChatConfig.model_validate({"max_tokens": ""}).max_tokens is None
        assert cfg.system_prompt  # non-empty

    def test_chat_config_string_numeric_parsing(self):
        """ChatConfig tolerates stringified numbers (how DynamoDB stores them)."""
        cfg = ChatConfig.model_validate(
            {
                "enabled": True,
                "model": "us.anthropic.claude-sonnet-4-6:1m",
                "temperature": "0.2",
                "top_k": "5",
                "top_p": "0.1",
                "max_tokens": "2048",
            }
        )
        assert cfg.temperature == 0.2
        assert cfg.top_k == 5.0
        assert cfg.max_tokens == 2048

    def test_chat_config_on_idp_config(self):
        """IDPConfig exposes the chat section with defaults."""
        cfg = IDPConfig.model_validate({})
        assert cfg.chat is not None
        assert cfg.chat.enabled is True
        assert cfg.chat.model  # non-empty string

    def test_chat_config_override_via_idp_config(self):
        """User overrides to chat section flow through IDPConfig validation."""
        cfg = IDPConfig.model_validate(
            {"chat": {"model": "us.amazon.nova-pro-v1:0", "max_tokens": "8192"}}
        )
        assert cfg.chat.model == "us.amazon.nova-pro-v1:0"
        assert cfg.chat.max_tokens == 8192

    def test_chat_config_temperature_range_enforced(self):
        """temperature must be within [0, 1] like other model sections."""
        with pytest.raises(Exception):  # Pydantic ValidationError
            ChatConfig.model_validate({"temperature": 2.0})


class TestPipelineHookPreservation:
    """Feature Platform pipeline hooks are stored inline in a config version
    under `<step>.postHook`. The host's dispatcher reads them from the raw
    DynamoDB row, but several write paths round-trip the config through
    IDPConfig validation (Save-as-Version, updateConfiguration, and the
    sparse-config auto-migration in ConfigurationManager). If `postHook` were
    not a declared field, extra="ignore" would silently drop it on those
    round-trips, leaving the dispatcher with no hook to invoke (symptom: a
    feature's post-step hook never fires, e.g. the Claims Dashboard stays
    empty). These tests lock the field in on every hookable step.
    """

    _HOOK = {
        "featureId": "sample-health-insurance-review",
        "arn": "arn:aws:lambda:us-west-2:111122223333:function:ClaimStatusHook",
        "order": 100,
        "onError": "continue",
        "enabled": True,
    }
    # v0.6: the standalone `assessment` step config was retired (confidence folded
    # into extraction), so it no longer carries a postHook list.
    _STEPS = [
        "ocr",
        "classification",
        "extraction",
        "rule_validation",
        "summarization",
    ]

    def test_post_hook_survives_idp_config_round_trip_all_steps(self):
        cfg_dict = {step: {"postHook": [self._HOOK]} for step in self._STEPS}
        dumped = IDPConfig.model_validate(cfg_dict).model_dump(mode="python")
        for step in self._STEPS:
            hooks = dumped[step]["postHook"]
            assert len(hooks) == 1, f"{step}.postHook dropped on round-trip"
            assert hooks[0]["arn"].endswith(":ClaimStatusHook")
            assert hooks[0]["featureId"] == "sample-health-insurance-review"
            assert hooks[0]["onError"] == "continue"
            assert hooks[0]["enabled"] is True

    def test_post_hook_defaults_to_empty_list(self):
        """No hooks configured → empty list, never None (dispatcher iterates it)."""
        cfg = IDPConfig.model_validate({})
        assert cfg.rule_validation.postHook == []
        assert cfg.classification.postHook == []

    def test_preprocessing_flat_hook_survives_round_trip(self):
        """The flat `preprocessing` section (arn/args/onError + generic args)
        must survive the IDPConfig round-trip — otherwise the PII Anonymization
        wizard/preset would lose the hook + args on Save-as-Version /
        applyFeatureConfigPreset, and the dispatcher would find no hook."""
        cfg_dict = {
            "preprocessing": {
                "enabled": True,
                "featureId": "pii-anonymizer",
                "arn": "arn:aws:lambda:us-west-2:111122223333:function:PiiHook",
                "onError": "fail",
                "args": [
                    {"key": "mode", "value": "redactcopy_and_stop"},
                    {"key": "model_id", "value": "us.amazon.nova-lite-v1:0"},
                ],
            }
        }
        dumped = IDPConfig.model_validate(cfg_dict).model_dump(mode="python")
        pp = dumped["preprocessing"]
        assert pp["enabled"] is True
        assert pp["arn"].endswith(":PiiHook")
        assert pp["onError"] == "fail"
        # generic args (feature config) preserved
        assert {"key": "mode", "value": "redactcopy_and_stop"} in pp["args"]
        assert {"key": "model_id", "value": "us.amazon.nova-lite-v1:0"} in pp["args"]

    def test_preprocessing_defaults(self):
        cfg = IDPConfig.model_validate({})
        assert cfg.preprocessing.enabled is False
        assert cfg.preprocessing.arn is None
        assert cfg.preprocessing.args == []

    def test_postprocessing_flat_hook_survives_round_trip(self):
        """The flat `postprocessing` section is the mirror of `preprocessing` and
        needs the same protection: dropped on a round-trip, the final delivery
        hook would silently stop running after any Save-as-Version /
        applyFeatureConfigPreset."""
        cfg_dict = {
            "postprocessing": {
                "enabled": True,
                "featureId": "delivery",
                "arn": "arn:aws:lambda:us-west-2:111122223333:function:DeliverHook",
                "onError": "continue",
                "args": [{"key": "endpoint", "value": "https://sap.example/ingest"}],
            }
        }
        dumped = IDPConfig.model_validate(cfg_dict).model_dump(mode="python")
        pp = dumped["postprocessing"]
        assert pp["enabled"] is True
        assert pp["arn"].endswith(":DeliverHook")
        assert pp["onError"] == "continue"
        assert pp["featureId"] == "delivery"
        assert {
            "key": "endpoint",
            "value": "https://sap.example/ingest",
        } in pp["args"]

    def test_postprocessing_defaults(self):
        """Inert by default: no ARN, disabled, empty args — the dispatcher finds
        no hook and the workflow tail is unchanged."""
        cfg = IDPConfig.model_validate({})
        assert cfg.postprocessing.enabled is False
        assert cfg.postprocessing.arn is None
        assert cfg.postprocessing.args == []
        assert cfg.postprocessing.allowDocumentUpdate is True

    def test_both_flat_hook_sections_coexist(self):
        """A config may carry both flat hooks; neither round-trip clobbers the
        other (they are separate top-level sections)."""
        cfg = IDPConfig.model_validate(
            {
                "preprocessing": {"enabled": True, "arn": "arn:pre", "onError": "fail"},
                "postprocessing": {"enabled": True, "arn": "arn:post"},
            }
        )
        dumped = cfg.model_dump(mode="python")
        assert dumped["preprocessing"]["arn"] == "arn:pre"
        assert dumped["preprocessing"]["onError"] == "fail"
        assert dumped["postprocessing"]["arn"] == "arn:post"
        # Default onError differs by intent: preprocessing gates, postprocessing
        # must not fail an already-processed document by accident.
        assert dumped["postprocessing"]["onError"] == "continue"

    def test_sparse_rule_validation_overlay_keeps_hook_and_merges_defaults(self):
        """The real failure mode: a sparse preset overlay carrying only
        rule_validation.postHook must keep the hook AND inherit classification
        defaults (system_prompt) once merged into a full IDPConfig."""
        cfg = IDPConfig.model_validate(
            {"rule_validation": {"enabled": True, "postHook": [self._HOOK]}}
        )
        assert len(cfg.rule_validation.postHook) == 1
        # classification still has its default model (not wiped out).
        assert cfg.classification.one_stage.llm.model


@pytest.mark.unit
class TestRuleClassesMigrationIsLoud:
    """Discarding user-supplied rule_classes must be logged (#600).

    `rule_classes` was renamed to `policy_classes` in v0.5.9. When BOTH keys are
    present the deprecated one is dropped — and because `rule_classes` is a
    known-deprecated key it does not trip the unknown-field warning either. A
    notebook user following the old guidance therefore lost their rules with no
    message anywhere.
    """

    def test_rename_when_only_legacy_key_present(self):
        cfg = IDPConfig(
            **{"rule_classes": [{"x-aws-idp-policy-type": "a", "rule_properties": {}}]}
        )
        assert len(cfg.policy_classes) == 1
        assert cfg.policy_classes[0]["x-aws-idp-policy-type"] == "a"

    def test_discarding_legacy_key_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            cfg = IDPConfig(
                **{
                    "policy_classes": [
                        {"x-aws-idp-policy-type": "kept", "rule_properties": {}}
                    ],
                    "rule_classes": [
                        {"x-aws-idp-policy-type": "dropped", "rule_properties": {}}
                    ],
                }
            )

        # policy_classes still wins — behavior is unchanged, only the silence is.
        assert [pc["x-aws-idp-policy-type"] for pc in cfg.policy_classes] == ["kept"]
        assert "DISCARDING 'rule_classes'" in caplog.text
        assert "policy_classes" in caplog.text


#: The spellings an operator reaches for when they mean "no page limit", plus the two
#: this list did not used to cover. `True` is the one that mattered: `bool` is an
#: `int` subclass, so an unquoted YAML `maxPagesForClassification: true` was read as a
#: one-page limit — and in the migration it took `sectionSplitting` to `disabled` with
#: it, turning boundary detection off for a setting the operator never wrote.
NO_PAGE_LIMIT_SPELLINGS = ["ALL", "all", "0", 0, "", None, "abc", -1, True, False]


@pytest.mark.unit
class TestTheFileShapeIsOnlyAcceptedWhenItCanBeHonoured:
    """`file_shape` used to accept three values it could not act on.

    Both settings are assertions about a file, and an assertion the pipeline cannot
    honour is worse than a refused one: the editor shows it, the operator believes
    it, and nothing happens. The two halves are treated differently on purpose, and
    the asymmetry is the point of this class.
    """

    @pytest.mark.parametrize("value", NO_PAGE_LIMIT_SPELLINGS)
    def test_a_value_that_is_not_a_page_count_classifies_every_page(self, value):
        from idp_common.config.models import FileShapeConfig

        assert (
            FileShapeConfig(maxPagesForClassification=value).maxPagesForClassification
            == "ALL"
        )

    def test_an_unusable_page_limit_says_so_rather_than_being_stored(self, caplog):
        """`'abc'` used to validate, be stored, and then be discarded twice over.

        Once by the `sectionSplitting` check and again by an `int()` inside a bare
        `except`, so the editor kept showing a limit that was never in force.
        """
        from idp_common.config.models import FileShapeConfig

        with caplog.at_level(logging.WARNING):
            shape = FileShapeConfig(maxPagesForClassification="abc")
        assert shape.maxPagesForClassification == "ALL"
        assert "maxPagesForClassification" in caplog.text

    def test_the_legacy_zero_takes_the_default_silently(self, caplog):
        """`0` has always meant ALL, so it is a spelling rather than a mistake."""
        from idp_common.config.models import FileShapeConfig

        with caplog.at_level(logging.WARNING):
            shape = FileShapeConfig(maxPagesForClassification=0)
        assert shape.maxPagesForClassification == "ALL"
        assert "maxPagesForClassification" not in caplog.text

    @pytest.mark.parametrize("value", [-5, "-5"])
    def test_a_negative_limit_is_the_same_answer_whichever_type_it_arrives_as(
        self, value
    ):
        """`-5` became ALL and `'-5'` was stored verbatim — one value, two answers."""
        from idp_common.config.models import FileShapeConfig

        assert (
            FileShapeConfig(maxPagesForClassification=value).maxPagesForClassification
            == "ALL"
        )

    @pytest.mark.parametrize(
        ("value", "expected"), [(3, "3"), ("3", "3"), (" 7 ", "7"), ("ALL", "ALL")]
    )
    def test_a_real_page_limit_is_kept(self, value, expected):
        from idp_common.config.models import FileShapeConfig

        assert (
            FileShapeConfig(maxPagesForClassification=value).maxPagesForClassification
            == expected
        )

    def test_an_unrecognised_section_splitting_is_refused(self):
        """Unlike the page limit, this one raises — see the validator's docstring.

        The two differ in both consequence and reachability. An unusable page limit
        falls back to "classify every page", which is the complete answer and what
        the runtime already did with it; an unrecognised `sectionSplitting` fell back
        to `llm_determined`, the expensive value, and changed where sections start.
        And a bad page limit can already sit in a stored profile because the
        validator stored it, while a bad `sectionSplitting` cannot, because the
        validator rewrote it before it was saved.
        """
        from idp_common.config.models import FileShapeConfig

        with pytest.raises(ValueError, match="sectionSplitting"):
            FileShapeConfig(sectionSplitting="bogus")

    @pytest.mark.parametrize("value", ["", "   ", None])
    def test_clearing_section_splitting_takes_the_default_silently(self, value, caplog):
        """Every sibling validator treats empty as "use the default", and now so does
        this one: clearing the field in the editor used to print a spurious
        "Invalid sectionSplitting value ''"."""
        from idp_common.config.models import FileShapeConfig

        with caplog.at_level(logging.WARNING):
            shape = FileShapeConfig(sectionSplitting=value)
        assert shape.sectionSplitting == "llm_determined"
        assert "sectionSplitting" not in caplog.text

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("disabled", "disabled"),
            ("DISABLED", "disabled"),
            (" page ", "page"),
            ("llm_determined", "llm_determined"),
        ],
    )
    def test_a_recognised_value_is_normalised(self, value, expected):
        from idp_common.config.models import FileShapeConfig

        assert FileShapeConfig(sectionSplitting=value).sectionSplitting == expected


@pytest.mark.unit
class TestReasoningEffortIsChecked:
    """An unrecognised effort level used to reach Bedrock verbatim.

    The legal values are family-specific, were written out in the field's own
    description, and were enforced nowhere — so a typo was accepted, **stored**, and
    either failed the document at the provider or was dropped by a family that
    ignores effort, leaving the typo to surface on the next model change.

    ⚠️ **It substitutes the default rather than raising, and that is not a weaker
    version of the sibling `sectionSplitting` check — it is the opposite situation.**
    Because this field never had a validator, a typo is already storable and may sit
    in a profile right now; raising would make that profile fail validation on read,
    with no fallback to return, so every document would fail at config load. The
    `sectionSplitting` validator normalised on the way in, so a bad value could never
    be stored and raising there cannot wedge anything. See
    `TestAStoredTypoDoesNotWedgeAProfile` in `test_v08_to_v09_migration.py` for the
    read path that makes this concrete.
    """

    @pytest.mark.parametrize(
        "value", ["none", "minimal", "low", "medium", "high", "xhigh", "max", "HIGH"]
    )
    def test_every_documented_level_is_accepted(self, value):
        from idp_common.config.models import ClassificationModelSettings

        assert (
            ClassificationModelSettings(reasoning_effort=value).reasoning_effort
            == value.lower()
        )

    def test_a_level_no_family_accepts_is_replaced_and_reported(self, caplog):
        from idp_common.config.models import (
            DEFAULT_REASONING_EFFORT,
            ClassificationModelSettings,
        )

        with caplog.at_level(logging.WARNING):
            settings = ClassificationModelSettings(reasoning_effort="bogus")
        assert settings.reasoning_effort == DEFAULT_REASONING_EFFORT
        assert "reasoning_effort" in caplog.text
        assert "bogus" in caplog.text

    @pytest.mark.parametrize("value", ["", None])
    def test_clearing_it_takes_the_default_silently(self, value, caplog):
        from idp_common.config.models import (
            DEFAULT_REASONING_EFFORT,
            ClassificationModelSettings,
        )

        with caplog.at_level(logging.WARNING):
            settings = ClassificationModelSettings(reasoning_effort=value)
        assert settings.reasoning_effort == DEFAULT_REASONING_EFFORT
        assert "reasoning_effort" not in caplog.text

    def test_the_checked_set_is_pinned_against_casual_change(self):
        """A literal, and deliberately only that.

        ⚠️ This does **not** establish parity with the editor's picklist — it cannot,
        because it never opens the template. What it buys is that widening the set
        here is a visible edit rather than a one-character change. The parity
        assertion is `test_the_reasoning_effort_picklist_matches_the_checked_set` in
        `scripts/sdlc/tests/test_config_ui_schema_parity.py`, which reads the
        template's own Schema block; this docstring used to claim that job and the
        assertion below has never done it.
        """
        from idp_common.config.models import REASONING_EFFORT_VALUES

        assert set(REASONING_EFFORT_VALUES) == {
            "none",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }


@pytest.mark.unit
class TestDirectConstructionRunsTheWholeMigrationChain:
    """`ClassificationConfig(**flat)` must not lose what `IDPConfig(**flat)` keeps.

    The validator's own docstring says it exists so that "neither route loses a
    setting". It ran only the last hop, so for a v0.7 **holistic** configuration
    `classificationMethod` survived that hop and was then dropped by
    `extra="ignore"` — leaving `granularity: page` and the shipped page prompt in
    place of the operator's hand-written packet one.
    """

    FLAT_HOLISTIC = {
        "classificationMethod": "textbasedHolisticClassification",
        "model": "us.amazon.nova-2-lite-v1:0",
        "task_prompt": (
            "give me segments with ordinal_start_page and ordinal_end_page "
            "{CLASS_NAMES_AND_DESCRIPTIONS} {DOCUMENT_TEXT}"
        ),
        "system_prompt": "split packets",
    }

    def test_a_flat_holistic_config_becomes_packet_granularity(self):
        from idp_common.config.models import ClassificationConfig

        cfg = ClassificationConfig(**self.FLAT_HOLISTIC)
        assert cfg.one_stage.granularity == "packet"
        assert "ordinal_start_page" in cfg.one_stage.packet.task_prompt

    def test_both_routes_now_agree(self):
        """The property the docstring claims, asserted as an equality.

        Written as a comparison rather than two expected values on purpose: what is
        being promised is that the two entry points agree, so the assertion should
        fail if either one moves.
        """
        from idp_common.config.models import ClassificationConfig

        direct = ClassificationConfig(**self.FLAT_HOLISTIC).model_dump()
        through_wrapper = IDPConfig(
            classification=dict(self.FLAT_HOLISTIC)
        ).classification.model_dump()
        assert direct == through_wrapper
