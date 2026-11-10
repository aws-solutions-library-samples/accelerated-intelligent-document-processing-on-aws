# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Discovery output is kept to plain text, and the discovery agent's tools stay
inside the cluster and the sample size it was given.

Three groups:

* ``clean_text`` and the schema/rule walkers, on the inputs they exist for and on
  the inputs they must leave alone (``$schema`` URLs, comparison operators, numbers);
* the wiring: each place model output enters a profile or a job record calls them —
  the class write path, the rules write path, the cluster analysis result and the
  reflection report;
* the agent tools: ``get_document_image`` refuses a document outside the cluster and
  refuses new documents past ``max_sample_size``; the cluster tool answers for its
  own cluster only. Rules discovery refuses to save with no profile named.
"""

import io
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from idp_common.config.models import IDPConfig
from idp_common.discovery import discovered_text
from idp_common.discovery.classes_discovery import ClassesDiscovery
from idp_common.discovery.clustering_service import ClusteringService, ClusterResult
from idp_common.discovery.discovered_text import (
    MAX_DESCRIPTION_CHARS,
    MAX_EXAMPLES,
    clean_report,
    clean_rule_classes,
    clean_schema_text,
    clean_text,
)
from idp_common.discovery.discovery_agent import (
    ClusterAnalysisTool,
    DiscoveryAgent,
    DocumentVisualTool,
)
from idp_common.discovery.rules_discovery import (
    MISSING_VERSION_MESSAGE,
    RulesDiscovery,
    to_policy_class,
)

pytestmark = pytest.mark.unit

REMOTE = "remote.example"


class TestCleanText:
    @pytest.mark.parametrize(
        "raw",
        [
            f"Account number ![x](https://{REMOTE}/p.png?d=1) in the header",
            f"Account number <img src='https://{REMOTE}/p.png'> in the header",
            f"Account number https://{REMOTE}/a?b=c in the header",
            f"Account number http://{REMOTE} in the header",
            f"Account number //{REMOTE}/x in the header",
            f"Account number www.{REMOTE}/x in the header",
            f"Account number <https://{REMOTE}/x> in the header",
            "Account number data:image/png;base64,AAAA in the header",
            "Account number javascript:alert(1) in the header",
            f"Account number <!-- see https://{REMOTE} --> in the header",
            f"Account number ![x][ref] in the header\n[ref]: https://{REMOTE}/p.png",
            f"Account number <a href='https://{REMOTE}'></a> in the header",
            f"Account number HTTPS://{REMOTE}/x in the header",
            f"Account number https:/\\{REMOTE}/x in the header",
            f"Account number mailto:a@{REMOTE} in the header",
            f"Account number ![x](<https://{REMOTE}/a b>) in the header",
            f"Account number <IMG SRC=https://{REMOTE}> in the header",
        ],
    )
    def test_urls_images_and_html_are_removed(self, raw):
        cleaned = clean_text(raw)
        assert cleaned == "Account number in the header"
        assert REMOTE not in cleaned

    def test_a_link_keeps_its_text(self):
        assert (
            clean_text(f"See [the total box](https://{REMOTE}/x) on page 2")
            == "See the total box on page 2"
        )

    @pytest.mark.parametrize(
        "plain",
        [
            "Amount in USD, e.g. 1,234.56",
            "Value < 5 and > 2",
            "Form W-2.pdf, box 12a",
            "Is the CPT code within the valid range (30000-39999)?",
            "Date (MM/DD/YYYY) in the upper right",
            "Data: the reported amount, metadata:none",
        ],
    )
    def test_plain_text_is_unchanged(self, plain):
        assert clean_text(plain) == plain

    def test_length_is_capped(self):
        cleaned = clean_text("a" * (MAX_DESCRIPTION_CHARS * 3))
        assert len(cleaned) == MAX_DESCRIPTION_CHARS
        assert cleaned.endswith("…")

    def test_non_strings_are_returned_unchanged(self):
        assert clean_text(42) == 42
        assert clean_text(None) is None


class TestCleanSchemaText:
    def _schema(self):
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": "Invoice",
            "type": "object",
            "description": f"An invoice ![t](https://{REMOTE}/t.png)",
            "properties": {
                # A field NAMED description is a property, not text.
                "description": {
                    "type": "string",
                    "description": f"Line text <b>bold</b> https://{REMOTE}",
                    "examples": [f"Widget https://{REMOTE}/x", 12],
                },
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sku": {
                                "type": "string",
                                "pattern": "^https?://.*$",
                                "description": f"SKU www.{REMOTE}",
                            }
                        },
                    },
                },
            },
            "$defs": {
                "Addr": {"description": f"Address <script>x</script> //{REMOTE}/a"}
            },
        }

    def test_every_description_and_example_is_cleaned(self):
        schema = clean_schema_text(self._schema())
        text = repr(
            [
                schema["description"],
                schema["properties"]["description"],
                schema["properties"]["items"]["items"]["properties"]["sku"][
                    "description"
                ],
                schema["$defs"],
            ]
        )
        assert REMOTE not in text
        assert "<" not in text
        assert schema["properties"]["description"]["examples"] == ["Widget", 12]

    def test_structural_keys_are_left_alone(self):
        schema = clean_schema_text(self._schema())
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        sku = schema["properties"]["items"]["items"]["properties"]["sku"]
        assert sku["pattern"] == "^https?://.*$"
        assert "description" in schema["properties"]

    def test_examples_are_capped_in_number(self):
        schema = clean_schema_text({"examples": [str(i) for i in range(50)]})
        assert len(schema["examples"]) == MAX_EXAMPLES


class TestRuleText:
    def test_rule_descriptions_are_cleaned(self):
        rules = [
            {
                "x-aws-idp-rule-type": "billing",
                "description": f"Billing ![x](https://{REMOTE}/a.png)",
                "rule_properties": {
                    "rule_1": {"description": f"Is it valid? <img src=//{REMOTE}/b>"},
                    "rule_2": f"Bare text https://{REMOTE}",
                },
            }
        ]
        clean_rule_classes(rules)
        assert REMOTE not in repr(rules)
        assert rules[0]["rule_properties"]["rule_1"]["description"] == "Is it valid?"

    def test_the_persisted_policy_class_is_cleaned(self):
        persisted = to_policy_class(
            {
                "description": f"See https://{REMOTE}",
                "rule_properties": {
                    "r": {"description": f"<b>Q</b> www.{REMOTE}", "page": "V-3"}
                },
            },
            "billing",
        )
        assert REMOTE not in repr(persisted)
        assert persisted["rule_properties"]["r"]["page"] == "V-3"
        assert persisted["$schema"].startswith("https://json-schema.org/")


class TestReport:
    def test_report_keeps_markdown_structure_but_not_remote_content(self):
        report = (
            "# Review\n\n- Class **Invoice** looks complete\n"
            f"- ![chart](https://{REMOTE}/c.png?x=1)\n\n\n\nSee [docs](https://{REMOTE})."
        )
        cleaned = clean_report(report)
        assert REMOTE not in cleaned
        assert cleaned.startswith("# Review\n\n- Class **Invoice** looks complete\n")
        assert cleaned.endswith("See docs.")


class TestWiring:
    """Each place discovery output enters a profile or a job record cleans it."""

    @pytest.fixture
    def classes_service(self):
        with (
            patch("boto3.resource"),
            patch("idp_common.bedrock.BedrockClient"),
            patch(
                "idp_common.discovery.classes_discovery.ConfigurationReader"
            ) as reader,
            patch("idp_common.discovery.classes_discovery.ConfigurationManager"),
            patch.dict("os.environ", {"CONFIGURATION_TABLE_NAME": "t"}),
        ):
            reader.return_value.get_merged_configuration.return_value = IDPConfig()
            svc = ClassesDiscovery(
                input_bucket="b", input_prefix="d.pdf", region="us-west-2", version="v1"
            )
            svc.config_manager = MagicMock()
            svc.config_manager.get_raw_configuration.return_value = {
                "classes": [
                    {
                        "$id": "Authored",
                        "description": "Authored text with https://docs.example/x",
                    }
                ]
            }
            return svc

    def test_the_class_write_path_cleans_the_discovered_class_only(
        self, classes_service
    ):
        classes_service._merge_and_save_class(
            {
                "$id": "Invoice",
                "type": "object",
                "description": f"Invoice ![x](https://{REMOTE}/x.png)",
                "properties": {
                    "total": {
                        "type": "number",
                        "description": f"<i>Total</i> https://{REMOTE}",
                    }
                },
            }
        )
        saved = classes_service.config_manager.save_raw_configuration.call_args[0][1]
        by_id = {c["$id"]: c for c in saved["classes"]}
        assert REMOTE not in repr(by_id["Invoice"])
        assert by_id["Invoice"]["properties"]["total"]["description"] == "Total"
        # A class a person wrote is not rewritten by discovery.
        assert (
            by_id["Authored"]["description"]
            == "Authored text with https://docs.example/x"
        )

    def test_the_cluster_result_is_cleaned(self, monkeypatch):
        agent = DiscoveryAgent(max_sample_size=2)
        strands_agent = MagicMock()
        strands_agent.return_value.structured_output.model_dump.return_value = {
            "classification": "Invoice",
            "json_schema": {"description": f"Invoice ![x](https://{REMOTE}/a.png)"},
        }
        monkeypatch.setattr(agent, "_create_agent", lambda *a: strands_agent)
        result = MagicMock()
        result.cluster_sizes = {0: 2}
        result.get_cluster_indices.return_value = [0, 1]
        service = MagicMock()
        service.sample_cluster.return_value = [0, 1]
        dc = agent.analyze_cluster(0, result, {}, service)
        assert dc.error is None
        assert dc.json_schema == {"description": "Invoice"}

    def test_the_reflection_report_is_cleaned(self):
        client = MagicMock()
        client.extract_text_from_response.return_value = (
            f"Fine. ![x](https://{REMOTE}/a.png)"
        )
        agent = DiscoveryAgent(bedrock_client=client)
        assert agent.reflect([]) == "Fine."

    def test_extracted_rules_are_cleaned_before_save(self, monkeypatch):
        d = RulesDiscovery(input_bucket="b", input_prefix="p.pdf", config=IDPConfig())
        d.bedrock_client = MagicMock()
        d.version = "v1"
        d.config_manager = MagicMock()
        saved = []
        monkeypatch.setattr(d, "_save_rules_to_config", lambda r: saved.append(r) or [])
        monkeypatch.setattr(
            "idp_common.discovery.rules_discovery.S3Util.get_bytes", lambda **k: b"%PDF"
        )
        monkeypatch.setattr(
            d,
            "_extract_rules",
            lambda *a: [
                {
                    "x-aws-idp-rule-type": "r",
                    "rule_properties": {
                        "rule_1": {"description": f"Q? https://{REMOTE}"}
                    },
                }
            ],
        )
        result = d.discovery_rules_from_document("b", "p.pdf")
        assert REMOTE not in repr(result["rules"])
        assert REMOTE not in repr(saved)


class TestRulesDiscoveryNamesItsProfile:
    def _discovery(self):
        d = RulesDiscovery(input_bucket="b", input_prefix="p.pdf", config=IDPConfig())
        d.bedrock_client = MagicMock()
        d.config_manager = MagicMock()
        d.version = None
        return d

    def test_save_with_no_version_is_refused_and_writes_nothing(self):
        d = self._discovery()
        d.config_manager.list_config_versions.return_value = [
            {"versionName": "production", "isActive": True}
        ]
        with pytest.raises(ValueError, match="no fallback"):
            d._save_rules_to_config(
                [{"x-aws-idp-rule-type": "r", "rule_properties": {}}]
            )
        d.config_manager.save_configuration.assert_not_called()

    def test_the_run_is_refused_before_the_model_is_called(self, monkeypatch):
        d = self._discovery()
        extract = MagicMock()
        monkeypatch.setattr(d, "_extract_rules", extract)
        with pytest.raises(ValueError) as excinfo:
            d.discovery_rules_from_document("b", "p.pdf")
        assert str(excinfo.value) == MISSING_VERSION_MESSAGE
        extract.assert_not_called()

    def test_a_named_version_is_saved_into(self):
        d = self._discovery()
        d.version = "v1"
        d.config_manager.get_configuration.return_value = None
        d._save_rules_to_config([{"x-aws-idp-rule-type": "r", "rule_properties": {}}])
        assert d.config_manager.save_configuration.call_args.kwargs["version"] == "v1"

    def test_local_use_with_no_profile_still_works(self):
        d = RulesDiscovery(input_bucket="b", input_prefix="p.pdf", config=IDPConfig())
        assert d.config_manager is None  # nothing is saved, so nothing is required


def _png():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (8, 8)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def cluster_result():
    from scipy.spatial import KDTree

    embeddings = np.array(
        [[1.0, 0.0], [1.1, 0.1], [1.2, 0.0], [0.0, 1.0], [0.1, 1.1], [0.0, 1.2]]
    )
    labels = np.array([0, 0, 0, 1, 1, 1])
    return ClusterResult(
        cluster_labels=labels,
        num_clusters=2,
        cluster_sizes={0: 3, 1: 3},
        centroids={0: embeddings[0:3].mean(axis=0), 1: embeddings[3:6].mean(axis=0)},
        embeddings=embeddings,
        kdtree=KDTree(embeddings),
    )


class TestToolLimits:
    def test_a_document_outside_the_cluster_is_refused(self):
        img = _png()
        tool = DocumentVisualTool({0: img, 3: img}, allowed_doc_ids=[0, 1, 2])
        assert tool.get_document_image(0)["status"] == "success"
        refused = tool.get_document_image(3)
        assert refused["status"] == "error"
        assert "not in the cluster" in refused["content"][0]["text"]

    def test_new_documents_past_the_sample_size_are_refused(self):
        img = _png()
        tool = DocumentVisualTool({i: img for i in range(5)}, max_views=2)
        assert tool.get_document_image(0)["status"] == "success"
        assert tool.get_document_image(1)["status"] == "success"
        assert tool.get_document_image(0)["status"] == "success"  # a re-view is free
        refused = tool.get_document_image(2)
        assert refused["status"] == "error"
        assert "sample limit of 2" in refused["content"][0]["text"]

    def test_the_cluster_tool_answers_for_its_own_cluster_only(self, cluster_result):
        service = ClusteringService(min_cluster_size=2, num_sample_documents=3)
        tool = ClusterAnalysisTool(cluster_result, service, cluster_id=0)
        assert tool.get_cluster_docids_by_distance_to_centroid(0)["status"] == "success"
        assert tool.get_cluster_docids_by_distance_to_centroid(1)["status"] == "error"

    def test_analyze_cluster_binds_both_tools(self, cluster_result, monkeypatch):
        """The tools the agent is built with carry this cluster and the sample size."""
        img = _png()
        agent = DiscoveryAgent(max_sample_size=2)
        built = {}

        def fake_create(cluster_tool, visual_tool):
            built["cluster"], built["visual"] = cluster_tool, visual_tool
            raise RuntimeError("stop after construction")

        monkeypatch.setattr(agent, "_create_agent", fake_create)
        service = ClusteringService(min_cluster_size=2, num_sample_documents=3)
        agent.analyze_cluster(1, cluster_result, {i: img for i in range(6)}, service)
        visual = built["visual"]
        assert visual.get_document_image(0)["status"] == "error"  # cluster 0 member
        assert visual.get_document_image(3)["status"] == "success"
        assert visual.get_document_image(4)["status"] == "success"
        assert visual.get_document_image(5)["status"] == "error"  # past the limit
        assert (
            built["cluster"].get_cluster_docids_by_distance_to_centroid(0)["status"]
            == "error"
        )


def test_the_module_has_no_dependency_beyond_the_standard_library():
    """Imported by the class, rules and agent write paths alike; it must stay cheap."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(discovered_text.__file__).read_text())
    imported = {
        (n.module or "").split(".")[0]
        if isinstance(n, ast.ImportFrom)
        else a.name.split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
        for a in (n.names if isinstance(n, ast.Import) else [None])
    }
    assert imported <= {"__future__", "re", "typing"}
