# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for multi-document discovery config saving."""

from unittest.mock import MagicMock, patch

import pytest

from idp_common.discovery.discovery_agent import DiscoveredClass
from idp_common.discovery.multi_document_discovery import MultiDocumentDiscovery


@pytest.mark.unit
class TestSaveToConfigReportedNames:
    """The names returned by ``save_to_config`` must exist in the config.

    ``_merge_and_save_class`` normalizes the class id in place, so the raw LLM
    label in ``DiscoveredClass.classification`` no longer names what was saved.
    Reporting it would tell the user about a class the config does not contain.
    """

    @pytest.fixture
    def discovery(self):
        with patch(
            "idp_common.discovery.multi_document_discovery.BedrockClient"
        ) as mock_client:
            mock_client.return_value = MagicMock()
            return MultiDocumentDiscovery(region="us-west-2")

    def _save(self, discovery, discovered_class):
        """Run save_to_config with the real _merge_and_save_class mutation."""
        with patch(
            "idp_common.discovery.classes_discovery.ClassesDiscovery"
        ) as mock_cd:
            instance = mock_cd.return_value
            # Mirror the in-place normalization the real merge performs.
            instance._merge_and_save_class.side_effect = lambda schema: schema.update(
                {"$id": "Bank-Statement", "x-aws-idp-document-type": "Bank-Statement"}
            )
            return discovery.save_to_config(
                discovered_classes=[discovered_class],
                config_version="v1",
                input_bucket="b",
                input_prefix="p/",
            )

    def test_reported_name_is_the_saved_id_not_the_raw_label(self, discovery):
        saved = self._save(
            discovery,
            DiscoveredClass(
                cluster_id=0,
                classification="Bank Statement",
                json_schema={
                    "$id": "Bank Statement",
                    "x-aws-idp-document-type": "Bank Statement",
                    "type": "object",
                    "properties": {},
                },
                document_count=3,
                sample_doc_ids=[0, 1, 2],
            ),
        )

        assert saved == ["Bank-Statement"]

    def test_classification_is_sanitized_when_the_schema_carries_no_id(self, discovery):
        """Fallback path: whatever is reported must still be a valid class id."""
        with patch(
            "idp_common.discovery.classes_discovery.ClassesDiscovery"
        ) as mock_cd:
            mock_cd.return_value._merge_and_save_class.return_value = None
            saved = discovery.save_to_config(
                discovered_classes=[
                    DiscoveredClass(
                        cluster_id=7,
                        classification="Bank Statement",
                        json_schema={"type": "object", "properties": {}},
                        document_count=1,
                        sample_doc_ids=[0],
                    )
                ],
                config_version="v1",
                input_bucket="b",
                input_prefix="p/",
            )

        assert saved == ["Bank-Statement"]


def _embedded_corpus():
    """Seven embedded documents: two tight groups of three and one outlier.

    Clustered for real by ``ClusteringService`` (KMeans with silhouette-chosen k,
    ``min_cluster_size=2``), this yields labels ``[0, 0, 0, 1, 1, 1, -1]``: the
    outlier forms a cluster of one and is relabelled noise. Two more documents'
    embeddings failed, so they are in ``failed_keys`` and are never clustered.
    The two counts differ on purpose, so no assertion can pass with one sourced
    from the other.
    """
    import numpy as np

    from idp_common.discovery.embedding_service import EmbeddingResult

    rng = np.random.default_rng(0)
    group_a = np.array([1.0, 0.0, 0.0, 0.0])
    group_b = np.array([0.0, 1.0, 0.0, 0.0])
    outlier = np.array([0.0, 0.0, 1.0, 0.0])
    embeddings = np.vstack(
        [group_a + rng.normal(0, 0.01, 4) for _ in range(3)]
        + [group_b + rng.normal(0, 0.01, 4) for _ in range(3)]
        + [outlier]
    )
    return EmbeddingResult(
        embeddings=embeddings,
        valid_keys=["a1.pdf", "a2.pdf", "a3.pdf", "b1.pdf", "b2.pdf", "b3.pdf"]
        + ["odd.pdf"],
        failed_keys=["broken.pdf", "corrupt.pdf"],
        embedding_dim=4,
    )


@pytest.mark.unit
class TestNoiseIsReportedSeparatelyFromFailedEmbeddings:
    """Noise and failed embeddings are different documents and are reported apart.

    A noise document was embedded and clustered, but its cluster was smaller than
    ``min_cluster_size`` and was dropped, so no discovered class covers it. A
    failed-embedding document never reached clustering. The result used to carry
    only the second, and the SDK reported it as the first.
    """

    @pytest.fixture
    def discovery(self):
        with patch("idp_common.discovery.multi_document_discovery.BedrockClient"):
            return MultiDocumentDiscovery(region="us-west-2")

    @pytest.fixture
    def run(self, discovery):
        corpus = _embedded_corpus()
        paths = corpus.valid_keys + corpus.failed_keys
        discovery.list_local_documents = MagicMock(return_value=paths)
        discovery.generate_embeddings_local = MagicMock(return_value=corpus)
        discovery._load_local_images = MagicMock(return_value={})
        discovery.discovery_agent = MagicMock()
        discovery.discovery_agent.analyze_clusters.return_value = []
        discovery.reflect = MagicMock(return_value="# report")
        return discovery.run_local_pipeline(document_dir="unused")

    def test_the_noise_cluster_document_is_listed_as_noise(self, run):
        assert run.num_clusters == 2
        assert run.noise_document_keys == ["odd.pdf"]
        assert run.num_noise_documents == 1

    def test_the_failed_embedding_is_listed_on_its_own(self, run):
        assert run.failed_embedding_keys == ["broken.pdf", "corrupt.pdf"]
        assert run.num_failed_embeddings == 2
        assert "broken.pdf" not in run.noise_document_keys

    def test_both_lists_reach_the_serialised_result(self, run):
        as_dict = run.to_dict()
        assert as_dict["noise_document_keys"] == ["odd.pdf"]
        assert as_dict["num_noise_documents"] == 1
        assert as_dict["failed_embedding_keys"] == ["broken.pdf", "corrupt.pdf"]
        assert as_dict["num_failed_embeddings"] == 2

    def test_the_s3_pipeline_reports_noise_the_same_way(self, discovery):
        corpus = _embedded_corpus()
        discovery.list_documents = MagicMock(
            return_value=corpus.valid_keys + corpus.failed_keys
        )
        discovery.generate_embeddings = MagicMock(return_value=corpus)
        discovery._load_images_for_analysis = MagicMock(return_value={})
        discovery.discovery_agent = MagicMock()
        discovery.discovery_agent.analyze_clusters.return_value = []
        discovery.reflect = MagicMock(return_value="# report")

        result = discovery.run_full_pipeline(bucket="b", prefix="p/")

        assert result.noise_document_keys == ["odd.pdf"]
        assert result.failed_embedding_keys == ["broken.pdf", "corrupt.pdf"]

    def test_a_label_mismatch_stops_the_run_before_anything_is_saved(self, discovery):
        """The noise keys are derived right after clustering, ahead of the save."""
        import numpy as np

        corpus = _embedded_corpus()
        discovery.list_local_documents = MagicMock(return_value=corpus.valid_keys)
        discovery.generate_embeddings_local = MagicMock(return_value=corpus)
        discovery.cluster_documents = MagicMock(
            return_value=MagicMock(cluster_labels=np.array([0, -1]))
        )
        discovery.save_to_config = MagicMock()
        discovery.discovery_agent = MagicMock()

        with pytest.raises(ValueError, match="cluster labels"):
            discovery.run_local_pipeline(document_dir="unused", config_version="v1")
        discovery.save_to_config.assert_not_called()
        discovery.discovery_agent.analyze_clusters.assert_not_called()

    def test_a_result_built_the_old_way_still_constructs(self):
        """Readers and constructors that predate the noise fields keep working."""
        from idp_common.discovery.multi_document_discovery import (
            MultiDocDiscoveryResult,
        )

        result = MultiDocDiscoveryResult(
            discovered_classes=[],
            reflection_report="",
            total_documents=0,
            num_clusters=0,
            num_failed_embeddings=0,
            num_successful_schemas=0,
            num_failed_schemas=0,
        )
        assert result.noise_document_keys == []
        assert result.num_noise_documents == 0


@pytest.mark.unit
def test_noise_keys_refuse_labels_that_do_not_match_the_embedded_documents():
    """A label count that disagrees with the embedded keys is a bug, not noise."""
    import numpy as np

    from idp_common.discovery.multi_document_discovery import noise_document_keys

    corpus = _embedded_corpus()
    cluster_result = MagicMock(cluster_labels=np.array([0, -1]))
    with pytest.raises(ValueError, match="2 cluster labels for 7 embedded"):
        noise_document_keys(corpus, cluster_result)
