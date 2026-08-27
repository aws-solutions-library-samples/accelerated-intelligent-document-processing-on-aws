# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the ClusteringService."""

import numpy as np
import pytest

from idp_common.discovery.clustering_service import ClusteringService, ClusterResult


@pytest.fixture
def clustering_service():
    """Create a ClusteringService with test-friendly defaults."""
    return ClusteringService(
        min_cluster_size=2,
        num_sample_documents=3,
        random_state=42,
    )


def _make_clustered_embeddings(n_per_cluster=10, n_clusters=3, dim=8):
    """Create synthetic embeddings with clear cluster structure."""
    rng = np.random.RandomState(42)
    embeddings = []
    for i in range(n_clusters):
        center = rng.randn(dim) * 5
        cluster_points = center + rng.randn(n_per_cluster, dim) * 0.5
        embeddings.append(cluster_points)
    return np.vstack(embeddings)


class TestClusterResult:
    """Tests for ClusterResult dataclass."""

    def test_get_cluster_indices(self):
        """Test retrieving indices for a cluster."""
        labels = np.array([0, 0, 1, 1, 2, 2])
        embeddings = np.random.randn(6, 4)
        result = ClusterResult(
            cluster_labels=labels,
            num_clusters=3,
            cluster_sizes={0: 2, 1: 2, 2: 2},
            centroids={},
            embeddings=embeddings,
            kdtree=None,
        )
        assert result.get_cluster_indices(0) == [0, 1]
        assert result.get_cluster_indices(1) == [2, 3]
        assert result.get_cluster_indices(2) == [4, 5]

    def test_get_cluster_ids(self):
        """Test getting valid cluster IDs."""
        result = ClusterResult(
            cluster_labels=np.array([0, 0, -1, 1, 1]),
            num_clusters=2,
            cluster_sizes={-1: 1, 0: 2, 1: 2},
            centroids={},
            embeddings=np.random.randn(5, 4),
            kdtree=None,
        )
        assert result.get_cluster_ids() == [0, 1]

    def test_to_serializable(self):
        """Test JSON serialization."""
        result = ClusterResult(
            cluster_labels=np.array([0, 0, 1, 1]),
            num_clusters=2,
            cluster_sizes={0: 2, 1: 2},
            centroids={},
            embeddings=np.random.randn(4, 4),
            kdtree=None,
        )
        serialized = result.to_serializable()
        assert serialized["num_clusters"] == 2
        assert serialized["cluster_ids"] == [0, 1]
        assert len(serialized["cluster_labels"]) == 4


class TestClusterArtifactRoundTrip:
    """Tests for the to_artifact()/from_artifact() S3 hand-off.

    The discovery workflow writes this artifact in the clustering step and reads
    it back in the (separately invoked) analyze step. It used to be a pickle,
    which made the load an arbitrary-code-execution sink; these tests pin the
    JSON contract that replaced it, including the bits JSON cannot represent
    natively — numpy arrays and integer dict keys.
    """

    @pytest.fixture
    def result(self, clustering_service):
        return clustering_service.cluster(_make_clustered_embeddings())

    def test_artifact_is_json_serializable(self, result):
        """No numpy scalars may leak through — json.dumps() rejects them."""
        import json

        blob = json.dumps(result.to_artifact())
        assert json.loads(blob)["num_clusters"] == result.num_clusters

    def test_round_trip_preserves_cluster_membership(self, result):
        """A rebuilt result must answer every query identically to the source."""
        import json

        from scipy.spatial import KDTree

        data = json.loads(json.dumps(result.to_artifact()))
        restored = ClusterResult.from_artifact(
            data, embeddings=result.embeddings, kdtree=KDTree(result.embeddings)
        )

        assert restored.num_clusters == result.num_clusters
        assert restored.cluster_sizes == result.cluster_sizes
        assert restored.get_cluster_ids() == result.get_cluster_ids()
        for cluster_id in result.get_cluster_ids():
            assert restored.get_cluster_indices(
                cluster_id
            ) == result.get_cluster_indices(cluster_id)
            assert np.allclose(
                restored.centroids[cluster_id], result.centroids[cluster_id]
            )

    def test_round_trip_restores_integer_keys(self, result):
        """JSON stringifies dict keys; from_artifact() must cast them back.

        get_cluster_ids() compares keys with ``cid >= 0``, which raises on str.
        """
        import json

        from scipy.spatial import KDTree

        data = json.loads(json.dumps(result.to_artifact()))
        assert all(isinstance(k, str) for k in data["cluster_sizes"])
        assert all(isinstance(k, str) for k in data["centroids"])

        restored = ClusterResult.from_artifact(
            data, embeddings=result.embeddings, kdtree=KDTree(result.embeddings)
        )
        assert all(isinstance(k, int) for k in restored.cluster_sizes)
        assert all(isinstance(k, int) for k in restored.centroids)

    def test_round_trip_supports_sampling(self, result, clustering_service):
        """The analyze step samples off the restored object, so that must work."""
        import json

        from scipy.spatial import KDTree

        data = json.loads(json.dumps(result.to_artifact()))
        restored = ClusterResult.from_artifact(
            data, embeddings=result.embeddings, kdtree=KDTree(result.embeddings)
        )
        cluster_id = result.get_cluster_ids()[0]
        assert clustering_service.sample_cluster(
            restored, cluster_id=cluster_id
        ) == clustering_service.sample_cluster(result, cluster_id=cluster_id)


class TestClusteringService:
    """Tests for ClusteringService."""

    def test_init_defaults(self):
        """Test default initialization."""
        service = ClusteringService()
        assert service.min_cluster_size == 2
        assert service.num_sample_documents == 3
        assert service.random_state == 42

    def test_cluster_well_separated(self, clustering_service):
        """Test clustering with well-separated clusters."""
        embeddings = _make_clustered_embeddings(n_per_cluster=10, n_clusters=3, dim=8)
        result = clustering_service.cluster(embeddings)

        assert result.num_clusters >= 2  # Should find at least 2 clusters
        assert result.num_clusters <= 5  # But not too many
        assert len(result.cluster_labels) == 30
        assert result.embeddings.shape == (30, 8)
        assert result.kdtree is not None

    def test_cluster_single_document(self, clustering_service):
        """Test clustering with a single document."""
        embeddings = np.array([[1.0, 2.0, 3.0]])
        result = clustering_service.cluster(embeddings)

        assert result.num_clusters == 1
        assert result.cluster_labels[0] == 0

    def test_cluster_two_documents(self):
        """Test clustering with two documents (min_cluster_size=1 to avoid noise filtering)."""
        service = ClusteringService(min_cluster_size=1)
        embeddings = np.array([[1.0, 0.0], [0.0, 1.0]])
        result = service.cluster(embeddings)

        assert result.num_clusters >= 1

    def test_cluster_empty_raises(self, clustering_service):
        """Test that clustering empty embeddings raises ValueError."""
        with pytest.raises(ValueError, match="Cannot cluster empty"):
            clustering_service.cluster(np.array([]))

    def test_cluster_wrong_dims_raises(self, clustering_service):
        """Test that 1D embeddings raise ValueError."""
        with pytest.raises(ValueError, match="Expected 2D"):
            clustering_service.cluster(np.array([1.0, 2.0, 3.0]))

    def test_sample_cluster_small(self, clustering_service):
        """Test sampling from a cluster smaller than num_samples."""
        embeddings = _make_clustered_embeddings(n_per_cluster=2, n_clusters=2, dim=4)
        result = clustering_service.cluster(embeddings)

        cluster_ids = result.get_cluster_ids()
        samples = clustering_service.sample_cluster(
            result, cluster_ids[0], num_samples=5
        )

        # Should return all documents (less than 5)
        assert len(samples) <= 5

    def test_sample_cluster_diverse(self, clustering_service):
        """Test that sampling includes diverse documents."""
        embeddings = _make_clustered_embeddings(n_per_cluster=20, n_clusters=2, dim=4)
        result = clustering_service.cluster(embeddings)

        cluster_ids = result.get_cluster_ids()
        samples = clustering_service.sample_cluster(
            result, cluster_ids[0], num_samples=3
        )

        assert len(samples) == 3
        # All samples should be unique
        assert len(set(samples)) == 3

    def test_get_docs_by_distance(self, clustering_service):
        """Test getting documents sorted by distance to centroid."""
        embeddings = _make_clustered_embeddings(n_per_cluster=10, n_clusters=2, dim=4)
        result = clustering_service.cluster(embeddings)

        cluster_ids = result.get_cluster_ids()
        docs = clustering_service.get_docs_by_distance_to_centroid(
            result, cluster_ids[0], max_docs=5
        )

        assert len(docs) <= 5
        # Should be sorted by distance
        distances = [d["distance"] for d in docs]
        assert distances == sorted(distances)

    def test_filter_small_clusters(self, clustering_service):
        """Test that small clusters are filtered as noise."""
        # Create labels with one small cluster
        labels = np.array([0, 0, 0, 0, 0, 1])  # cluster 1 has only 1 doc
        filtered = clustering_service._filter_small_clusters(labels)

        # Cluster 1 should be noise (-1)
        assert filtered[-1] == -1
        # Cluster 0 should be renumbered to 0
        assert all(filtered[:-1] == 0)

    def test_build_cluster_result(self, clustering_service):
        """Test building ClusterResult with centroids and KDTree."""
        embeddings = np.array(
            [
                [1.0, 0.0],
                [1.1, 0.0],
                [0.0, 1.0],
                [0.0, 1.1],
            ]
        )
        labels = np.array([0, 0, 1, 1])

        result = clustering_service._build_cluster_result(embeddings, labels)

        assert result.num_clusters == 2
        assert 0 in result.centroids
        assert 1 in result.centroids
        assert result.kdtree is not None
        # Centroid of cluster 0 should be near (1.05, 0.0)
        np.testing.assert_allclose(result.centroids[0], [1.05, 0.0], atol=0.01)
