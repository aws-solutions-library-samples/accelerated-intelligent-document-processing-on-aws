# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Unit tests for the Z3 rule-translation config loader: TranslatorConfig,
ValueExtractionConfig and ConfigLoader.load.

The two dataclasses validate in __post_init__, and the checks that matter most are
the placeholder checks. `task_prompt_template` is interpolated with `.format()`
later, so a template missing `{rule}` does not fail at load time in any visible
way — it produces a prompt with no rule in it, and the model answers about
nothing. Each required placeholder is therefore asserted individually.

`ConfigLoader.load` reads from disk and consults two environment variables, so
every case here writes a real YAML file to `tmp_path` rather than patching
`open`: the file-shape errors (absent, a directory, unparseable, not a mapping)
are the ones a deployment actually hits, and they only exist on the filesystem
path.
"""

import textwrap

import pytest
import yaml

from idp_common.rule_validation.z3.config_loader import (
    Config,
    ConfigLoader,
    TranslatorConfig,
    ValueExtractionConfig,
)

TRANSLATOR_TEMPLATE = "Translate {rule} against {data_example}"
EXTRACTION_TEMPLATE = (
    "{rule_description} / {natural_language_rule} / {parameters_json} / "
    "{data_type} / {data}"
)


def _translator_kwargs(**overrides):
    kwargs = {
        "model_id": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "temperature": 0.0,
        "max_tokens": 4096,
        "system_prompt": "You translate business rules into SMT-LIB.",
        "task_prompt_template": TRANSLATOR_TEMPLATE,
    }
    kwargs.update(overrides)
    return kwargs


def _extraction_kwargs(**overrides):
    kwargs = {
        "model_id": "us.amazon.nova-lite-v1:0",
        "temperature": 0.0,
        "max_tokens": 2048,
        "system_prompt": "You extract parameter values.",
        "task_prompt_template": EXTRACTION_TEMPLATE,
    }
    kwargs.update(overrides)
    return kwargs


def _example(**overrides):
    example = {
        "rule": "coverage / income <= 20",
        "data_example": '{"coverage": 100, "income": 50}',
        "output": {
            "parameters": [{"name": "coverage", "type": "Real"}],
            "path_mappings": [{"parameter_name": "coverage", "data_path": "a.b"}],
            "constraints": ["(<= (/ coverage income) 20)"],
        },
    }
    example.update(overrides)
    return example


@pytest.mark.unit
class TestTranslatorConfig:
    """TranslatorConfig: model settings, prompts and few-shot examples."""

    def test_a_valid_configuration_is_accepted(self):
        config = TranslatorConfig(**_translator_kwargs())
        assert config.few_shot_examples == []

    @pytest.mark.parametrize("model_id", ["", None])
    def test_an_empty_model_id_is_rejected(self, model_id):
        with pytest.raises(ValueError, match="model_id"):
            TranslatorConfig(**_translator_kwargs(model_id=model_id))

    @pytest.mark.parametrize("temperature", [0.0, 0.5, 1.0, 0, 1])
    def test_the_whole_legal_temperature_range_including_the_bounds(self, temperature):
        assert (
            TranslatorConfig(**_translator_kwargs(temperature=temperature)).temperature
            == temperature
        )

    @pytest.mark.parametrize("temperature", [-0.1, 1.1, 2.0])
    def test_a_temperature_outside_zero_to_one_is_rejected(self, temperature):
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            TranslatorConfig(**_translator_kwargs(temperature=temperature))

    def test_a_non_numeric_temperature_is_rejected(self):
        with pytest.raises(ValueError, match="must be a number"):
            TranslatorConfig(**_translator_kwargs(temperature="0.0"))

    @pytest.mark.parametrize("max_tokens", [0, -1])
    def test_a_non_positive_max_tokens_is_rejected(self, max_tokens):
        with pytest.raises(ValueError, match="must be positive"):
            TranslatorConfig(**_translator_kwargs(max_tokens=max_tokens))

    @pytest.mark.parametrize("max_tokens", [4096.0, "4096", None])
    def test_a_non_integer_max_tokens_is_rejected(self, max_tokens):
        with pytest.raises(ValueError, match="must be an integer"):
            TranslatorConfig(**_translator_kwargs(max_tokens=max_tokens))

    def test_a_boolean_max_tokens_is_accepted_as_the_integer_one(self):
        # `isinstance(True, int)` is True and `True > 0`, so `max_tokens: true` in
        # a YAML config passes both checks and asks the model for a one-token
        # response — a silently truncated answer rather than a config error. Pinned
        # rather than asserted-as-rejected because this is what the code does.
        # Z3Validator._bind_values guards the same bool-is-int hazard explicitly;
        # these validators do not.
        assert TranslatorConfig(**_translator_kwargs(max_tokens=True)).max_tokens == 1

    @pytest.mark.parametrize("value", ["", None])
    def test_an_empty_system_prompt_is_rejected(self, value):
        with pytest.raises(ValueError, match="system_prompt"):
            TranslatorConfig(**_translator_kwargs(system_prompt=value))

    @pytest.mark.parametrize("value", ["", None])
    def test_an_empty_task_prompt_template_is_rejected(self, value):
        with pytest.raises(ValueError, match="task_prompt_template"):
            TranslatorConfig(**_translator_kwargs(task_prompt_template=value))

    def test_a_template_without_the_rule_placeholder_is_rejected(self):
        # Without it the model is asked to translate nothing, and the response is
        # a plausible rule about the wrong thing.
        with pytest.raises(ValueError, match=r"'\{rule\}' placeholder"):
            TranslatorConfig(
                **_translator_kwargs(task_prompt_template="Translate {data_example}")
            )

    def test_a_template_without_the_data_example_placeholder_is_rejected(self):
        with pytest.raises(ValueError, match=r"'\{data_example\}' placeholder"):
            TranslatorConfig(
                **_translator_kwargs(task_prompt_template="Translate {rule}")
            )

    def test_few_shot_examples_must_be_a_list(self):
        with pytest.raises(ValueError, match="few_shot_examples must be a list"):
            TranslatorConfig(**_translator_kwargs(few_shot_examples={"a": 1}))

    def test_a_well_formed_example_is_accepted(self):
        config = TranslatorConfig(**_translator_kwargs(few_shot_examples=[_example()]))
        assert len(config.few_shot_examples) == 1

    def test_a_non_dictionary_example_is_rejected_with_its_index(self):
        with pytest.raises(ValueError, match=r"few_shot_examples\[1\]"):
            TranslatorConfig(
                **_translator_kwargs(few_shot_examples=[_example(), "not-a-dict"])
            )

    @pytest.mark.parametrize("field_name", ["rule", "data_example", "output"])
    def test_each_required_example_field_is_checked(self, field_name):
        example = _example()
        del example[field_name]
        with pytest.raises(ValueError, match=field_name):
            TranslatorConfig(**_translator_kwargs(few_shot_examples=[example]))

    def test_a_non_dictionary_example_output_is_rejected(self):
        with pytest.raises(ValueError, match=r"output must be a dictionary"):
            TranslatorConfig(
                **_translator_kwargs(few_shot_examples=[_example(output="text")])
            )

    @pytest.mark.parametrize(
        "field_name", ["parameters", "path_mappings", "constraints"]
    )
    def test_each_required_example_output_field_is_checked(self, field_name):
        example = _example()
        del example["output"][field_name]
        with pytest.raises(ValueError, match=field_name):
            TranslatorConfig(**_translator_kwargs(few_shot_examples=[example]))


@pytest.mark.unit
class TestValueExtractionConfig:
    """ValueExtractionConfig: same shape, five required placeholders."""

    def test_a_valid_configuration_is_accepted(self):
        assert ValueExtractionConfig(**_extraction_kwargs()).max_tokens == 2048

    @pytest.mark.parametrize("model_id", ["", None])
    def test_an_empty_model_id_is_rejected(self, model_id):
        with pytest.raises(ValueError, match="model_id"):
            ValueExtractionConfig(**_extraction_kwargs(model_id=model_id))

    @pytest.mark.parametrize("temperature", [-0.1, 1.1])
    def test_a_temperature_outside_zero_to_one_is_rejected(self, temperature):
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            ValueExtractionConfig(**_extraction_kwargs(temperature=temperature))

    def test_a_non_numeric_temperature_is_rejected(self):
        with pytest.raises(ValueError, match="must be a number"):
            ValueExtractionConfig(**_extraction_kwargs(temperature=None))

    def test_a_non_positive_max_tokens_is_rejected(self):
        with pytest.raises(ValueError, match="must be positive"):
            ValueExtractionConfig(**_extraction_kwargs(max_tokens=0))

    def test_a_non_integer_max_tokens_is_rejected(self):
        with pytest.raises(ValueError, match="must be an integer"):
            ValueExtractionConfig(**_extraction_kwargs(max_tokens=2048.0))

    @pytest.mark.parametrize("value", ["", None])
    def test_an_empty_system_prompt_is_rejected(self, value):
        with pytest.raises(ValueError, match="system_prompt"):
            ValueExtractionConfig(**_extraction_kwargs(system_prompt=value))

    @pytest.mark.parametrize("value", ["", None])
    def test_an_empty_task_prompt_template_is_rejected(self, value):
        with pytest.raises(ValueError, match="task_prompt_template"):
            ValueExtractionConfig(**_extraction_kwargs(task_prompt_template=value))

    @pytest.mark.parametrize(
        "placeholder",
        [
            "{rule_description}",
            "{natural_language_rule}",
            "{parameters_json}",
            "{data_type}",
            "{data}",
        ],
    )
    def test_each_of_the_five_required_placeholders_is_checked(self, placeholder):
        # Each omission is checked on its own. A template missing only {data}
        # produces an extraction prompt with no document in it, which the model
        # answers from the rule text alone.
        template = EXTRACTION_TEMPLATE.replace(placeholder, "")
        with pytest.raises(ValueError, match="placeholder"):
            ValueExtractionConfig(**_extraction_kwargs(task_prompt_template=template))


def _config_document(**overrides):
    document = {
        "rule_translator": _translator_kwargs(few_shot_examples=[_example()]),
        "value_extraction": _extraction_kwargs(),
    }
    document.update(overrides)
    return document


def _write(tmp_path, document, name="translator_config.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return str(path)


@pytest.mark.unit
class TestConfigLoaderLoad:
    """ConfigLoader.load: the filesystem and environment path."""

    def test_a_complete_file_loads_both_sections(self, tmp_path):
        config = ConfigLoader.load(_write(tmp_path, _config_document()))
        assert isinstance(config, Config)
        assert isinstance(config.rule_translator, TranslatorConfig)
        assert isinstance(config.value_extraction, ValueExtractionConfig)
        assert config.rule_translator.max_tokens == 4096
        assert config.value_extraction.max_tokens == 2048

    def test_few_shot_examples_are_carried_through(self, tmp_path):
        config = ConfigLoader.load(_write(tmp_path, _config_document()))
        assert len(config.rule_translator.few_shot_examples) == 1

    def test_absent_few_shot_examples_default_to_empty(self, tmp_path):
        document = _config_document()
        del document["rule_translator"]["few_shot_examples"]
        config = ConfigLoader.load(_write(tmp_path, document))
        assert config.rule_translator.few_shot_examples == []

    def test_a_missing_file_names_the_path_and_the_default(self, tmp_path):
        with pytest.raises(FileNotFoundError) as excinfo:
            ConfigLoader.load(str(tmp_path / "absent.yaml"))
        assert "absent.yaml" in str(excinfo.value)
        assert "translator_config.yaml" in str(excinfo.value)

    def test_a_directory_is_rejected_rather_than_read(self, tmp_path):
        with pytest.raises(ValueError, match="not a file"):
            ConfigLoader.load(str(tmp_path))

    def test_unparseable_yaml_is_reported_as_a_parse_failure(self, tmp_path):
        path = tmp_path / "broken.yaml"
        path.write_text("rule_translator: [unclosed\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Failed to parse YAML"):
            ConfigLoader.load(str(path))

    @pytest.mark.parametrize("body", ["just a string\n", "- a\n- b\n", "\n"])
    def test_yaml_that_is_not_a_mapping_is_rejected(self, tmp_path, body):
        path = tmp_path / "wrong_shape.yaml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(ValueError, match="must contain a YAML dictionary"):
            ConfigLoader.load(str(path))

    @pytest.mark.parametrize("section", ["rule_translator", "value_extraction"])
    def test_a_missing_top_level_section_is_named(self, tmp_path, section):
        document = _config_document()
        del document[section]
        with pytest.raises(ValueError, match=section):
            ConfigLoader.load(_write(tmp_path, document))

    def test_both_missing_sections_are_named_in_one_message(self, tmp_path):
        path = tmp_path / "empty_map.yaml"
        path.write_text("unrelated: 1\n", encoding="utf-8")
        with pytest.raises(ValueError) as excinfo:
            ConfigLoader.load(str(path))
        message = str(excinfo.value)
        assert "rule_translator" in message and "value_extraction" in message

    @pytest.mark.parametrize("section", ["rule_translator", "value_extraction"])
    def test_a_section_that_is_not_a_mapping_is_rejected(self, tmp_path, section):
        document = _config_document(**{section: "a string"})
        with pytest.raises(
            ValueError, match=f"'{section}' section must be a dictionary"
        ):
            ConfigLoader.load(_write(tmp_path, document))

    @pytest.mark.parametrize(
        "field_name",
        [
            "model_id",
            "temperature",
            "max_tokens",
            "system_prompt",
            "task_prompt_template",
        ],
    )
    def test_each_required_translator_field_is_checked(self, tmp_path, field_name):
        document = _config_document()
        del document["rule_translator"][field_name]
        with pytest.raises(ValueError, match=field_name):
            ConfigLoader.load(_write(tmp_path, document))

    @pytest.mark.parametrize(
        "field_name",
        [
            "model_id",
            "temperature",
            "max_tokens",
            "system_prompt",
            "task_prompt_template",
        ],
    )
    def test_each_required_extraction_field_is_checked(self, tmp_path, field_name):
        document = _config_document()
        del document["value_extraction"][field_name]
        with pytest.raises(ValueError, match=field_name):
            ConfigLoader.load(_write(tmp_path, document))

    def test_a_dataclass_rejection_is_wrapped_and_attributed_to_its_section(
        self, tmp_path
    ):
        document = _config_document()
        document["rule_translator"]["temperature"] = 5.0
        with pytest.raises(ValueError, match="Invalid rule_translator configuration"):
            ConfigLoader.load(_write(tmp_path, document))

    def test_an_extraction_rejection_is_attributed_to_its_own_section(self, tmp_path):
        document = _config_document()
        document["value_extraction"]["max_tokens"] = -1
        with pytest.raises(ValueError, match="Invalid value_extraction configuration"):
            ConfigLoader.load(_write(tmp_path, document))

    def test_the_translator_model_id_can_be_overridden_by_environment(
        self, tmp_path, monkeypatch
    ):
        # The override exists so a non-US or GovCloud deployment can point at a
        # model id the bundled config does not name.
        monkeypatch.setenv(
            "Z3_TRANSLATOR_MODEL_ID", "eu.anthropic.claude-sonnet-4-5-v1:0"
        )
        config = ConfigLoader.load(_write(tmp_path, _config_document()))
        assert config.rule_translator.model_id == "eu.anthropic.claude-sonnet-4-5-v1:0"
        assert config.value_extraction.model_id == "us.amazon.nova-lite-v1:0"

    def test_the_extraction_model_id_can_be_overridden_independently(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("Z3_EXTRACTION_MODEL_ID", "eu.amazon.nova-lite-v1:0")
        config = ConfigLoader.load(_write(tmp_path, _config_document()))
        assert config.value_extraction.model_id == "eu.amazon.nova-lite-v1:0"
        assert config.rule_translator.model_id.startswith("us.")

    def test_an_unset_override_leaves_the_file_value_in_place(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("Z3_TRANSLATOR_MODEL_ID", raising=False)
        monkeypatch.delenv("Z3_EXTRACTION_MODEL_ID", raising=False)
        config = ConfigLoader.load(_write(tmp_path, _config_document()))
        assert config.rule_translator.model_id.startswith("us.anthropic")

    def test_a_multiline_yaml_prompt_survives_the_round_trip(self, tmp_path):
        # Prompts are written as YAML block scalars in the bundled config, which is
        # the one shape a naive loader mangles.
        path = tmp_path / "block.yaml"
        path.write_text(
            textwrap.dedent(
                """\
                rule_translator:
                  model_id: m1
                  temperature: 0.0
                  max_tokens: 100
                  system_prompt: |
                    Line one.
                    Line two.
                  task_prompt_template: |
                    {rule} and {data_example}
                value_extraction:
                  model_id: m2
                  temperature: 0.0
                  max_tokens: 100
                  system_prompt: extract
                  task_prompt_template: |
                    {rule_description} {natural_language_rule} {parameters_json} {data_type} {data}
                """
            ),
            encoding="utf-8",
        )
        config = ConfigLoader.load(str(path))
        assert config.rule_translator.system_prompt.splitlines() == [
            "Line one.",
            "Line two.",
        ]


@pytest.mark.unit
class TestDefaultConfig:
    """The bundled default config has to load and satisfy its own validators."""

    def test_the_default_path_points_at_a_file_that_ships(self):
        from pathlib import Path

        assert Path(ConfigLoader.get_default_config_path()).is_file()

    def test_the_bundled_default_config_loads(self):
        # This is the config every deployment uses unless it overrides one, so the
        # placeholder and range checks above are asserted against it too rather
        # than only against fixtures.
        config = ConfigLoader.load(ConfigLoader.get_default_config_path())
        assert config.rule_translator.model_id
        assert "{rule}" in config.rule_translator.task_prompt_template
        assert "{data}" in config.value_extraction.task_prompt_template
