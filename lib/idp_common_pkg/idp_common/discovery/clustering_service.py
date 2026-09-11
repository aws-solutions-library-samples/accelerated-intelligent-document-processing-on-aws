# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Clustering service for multi-document discovery.

Provides KMeans clustering and nearest-neighbor sampling using scikit-learn
and scipy for production-quality algorithms. Documents are clustered by
embedding similarity, and representative samples are selected for each cluster.
"""

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class ClusterResult:
    """Result of document clustering."""

    cluster_labels: np.ndarray
    """Cluster assignment for each document (-1 = noise/outlier)."""

    num_clusters: int
    """Number of clusters found (excluding noise)."""

    cluster_sizes: Dict[int, int]
    """Mapping of cluster_id -> number of documents in that cluster."""

    centroids: Dict[int, np.ndarray]
    """Mapping of cluster_id -> centroid vector."""

    embeddings: np.ndarray
    """The embeddings matrix used for clustering."""

    kdtree: Any
    """KDTree for efficient nearest-neighbor search."""

    def get_cluster_indices(self, cluster_id: int) -> List[int]:
        """Get document indices belonging to a cluster."""
        return np.where(self.cluster_labels == cluster_id)[0].tolist()

    def get_cluster_ids(self) -> List[int]:
        """Get list of valid cluster IDs (excluding noise)."""
        return sorted([cid for cid in self.cluster_sizes.keys() if cid >= 0])

    def to_serializable(self) -> Dict[str, Any]:
        """Convert to JSON-serializable dict for Step Functions state passing."""
        return {
            "cluster_labels": self.cluster_labels.tolist(),
            "num_clusters": self.num_clusters,
            "cluster_sizes": {str(k): v for k, v in self.cluster_sizes.items()},
            "cluster_ids": self.get_cluster_ids(),
        }

    def to_artifact(self) -> Dict[str, Any]:
        """Full JSON-safe form for the S3 hand-off between discovery steps.

        Unlike :meth:`to_serializable` (a compact status payload for the state
        machine) this keeps everything the analyze step needs to rebuild the
        object — notably the centroids. ``embeddings`` and ``kdtree`` are
        excluded: the embeddings ride their own ``.npy`` object and the KDTree is
        rebuilt from them.

        The format is JSON rather than pickle on purpose. The payload is only
        numbers, so pickle bought nothing, while making the load side an
        arbitrary-code-execution sink (bandit B301). Integer dict keys become
        strings because JSON has no integer keys; :meth:`from_artifact` casts
        them back.
        """
        return {
            "cluster_labels": self.cluster_labels.tolist(),
            "num_clusters": int(self.num_clusters),
            "cluster_sizes": {str(k): int(v) for k, v in self.cluster_sizes.items()},
            "centroids": {str(k): v.tolist() for k, v in self.centroids.items()},
            "embeddings_shape": list(self.embeddings.shape),
        }

    @classmethod
    def from_artifact(
        cls, data: Dict[str, Any], embeddings: np.ndarray, kdtree: Any
    ) -> "ClusterResult":
        """Rebuild a ClusterResult from :meth:`to_artifact` output.

        ``embeddings`` and ``kdtree`` are supplied by the caller because they are
        not carried in the artifact.
        """
        return cls(
            cluster_labels=np.asarray(data["cluster_labels"]),
            num_clusters=int(data["num_clusters"]),
            cluster_sizes={int(k): int(v) for k, v in data["cluster_sizes"].items()},
            centroids={int(k): np.asarray(v) for k, v in data["centroids"].items()},
            embeddings=embeddings,
            kdtree=kdtree,
        )


class ClusteringService:
    """
    Document clustering using KMeans and nearest-neighbor sampling.

    Uses scikit-learn for KMeans clustering and scipy for KDTree-based
    efficient nearest-neighbor search. Automatically determines the optimal
    number of clusters using the elbow method / silhouette analysis.

    Example:
        >>> service = ClusteringService(min_cluster_size=2)
        >>> result = service.cluster(embeddings)
        >>> samples = service.sample_cluster(result, cluster_id=0, num_samples=3)
    """

    def __init__(
        self,
        min_cluster_size: int = 2,
        max_clusters: Optional[int] = None,
        num_sample_documents: int = 3,
        random_state: int = 42,
    ):
        """
        Initialize the clustering service.

        Args:
            min_cluster_size: Minimum documents per cluster (smaller clusters are noise)
            max_clusters: Maximum number of clusters (default: auto-detect)
            num_sample_documents: Default number of documents to sample per cluster
            random_state: Random seed for reproducibility
        """
        self.min_cluster_size = min_cluster_size
        self.max_clusters = max_clusters
        self.num_sample_documents = num_sample_documents
        self.random_state = random_state

    def cluster(self, embeddings: np.ndarray) -> ClusterResult:
        """
        Cluster documents based on their embedding vectors.

        Automatically determines the optimal number of clusters using
        silhouette analysis, then applies KMeans clustering.

        Args:
            embeddings: 2D numpy array of shape (n_documents, embedding_dim)

        Returns:
            ClusterResult with cluster assignments and metadata

        Raises:
            ValueError: If embeddings is empty or has wrong dimensions
        """
        if embeddings.size == 0:
            raise ValueError("Cannot cluster empty embeddings")

        if embeddings.ndim != 2:
            raise ValueError(f"Expected 2D embeddings, got {embeddings.ndim}D")

        n_docs = embeddings.shape[0]
        logger.info(f"Clustering {n_docs} documents")

        if n_docs <= 1:
            # Single document: one cluster
            return self._build_cluster_result(
                embeddings=embeddings,
                labels=np.array([0]),
            )

        # Determine optimal number of clusters
        k = self._determine_optimal_k(embeddings)
        logger.info(f"Optimal number of clusters: {k}")

        # Run KMeans clustering
        labels = self._kmeans(embeddings, k)

        # Filter out small clusters as noise
        labels = self._filter_small_clusters(labels)

        return self._build_cluster_result(embeddings, labels)

    def sample_cluster(
        self,
        cluster_result: ClusterResult,
        cluster_id: int,
        num_samples: Optional[int] = None,
    ) -> List[int]:
        """
        Sample representative documents from a cluster.

        Selects the document closest to the centroid, plus diverse documents
        that are spread across the cluster.

        Args:
            cluster_result: Result from cluster() method
            cluster_id: Cluster to sample from
            num_samples: Number of documents to sample (default: num_sample_documents)

        Returns:
            List of document indices (into the original embeddings matrix)
        """
        num_samples = num_samples or self.num_sample_documents
        indices = cluster_result.get_cluster_indices(cluster_id)

        if len(indices) <= num_samples:
            return indices

        centroid = cluster_result.centroids[cluster_id]
        embeddings = cluster_result.embeddings

        # Find the document closest to centroid
        cluster_embeddings = embeddings[indices]
        distances_to_centroid = np.linalg.norm(cluster_embeddings - centroid, axis=1)
        sorted_by_distance = np.argsort(distances_to_centroid)

        # Start with closest to centroid
        selected = [indices[sorted_by_distance[0]]]

        # Add diverse documents using max-min distance
        remaining = set(range(len(indices))) - {sorted_by_distance[0]}

        while len(selected) < num_samples and remaining:
            max_min_dist = -1
            best_idx = None

            for r_idx in remaining:
                # Min distance from this candidate to all selected
                min_dist = min(
                    np.linalg.norm(cluster_embeddings[r_idx] - embeddings[s_idx])
                    for s_idx in selected
                )
                if min_dist > max_min_dist:
                    max_min_dist = min_dist
                    best_idx = r_idx

            if best_idx is not None:
                selected.append(indices[best_idx])
                remaining.remove(best_idx)

        return selected

    def get_docs_by_distance_to_centroid(
        self,
        cluster_result: ClusterResult,
        cluster_id: int,
        max_docs: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """
        Get documents sorted by distance to cluster centroid.

        Args:
            cluster_result: Result from cluster() method
            cluster_id: Cluster ID
            max_docs: Maximum documents to return (default: all)

        Returns:
            List of dicts with 'doc_id' and 'distance' keys, sorted by distance
        """
        indices = cluster_result.get_cluster_indices(cluster_id)
        centroid = cluster_result.centroids[cluster_id]
        embeddings = cluster_result.embeddings

        docs_with_distance = []
        for idx in indices:
            distance = float(np.linalg.norm(embeddings[idx] - centroid))
            docs_with_distance.append({"doc_id": idx, "distance": distance})

        docs_with_distance.sort(key=lambda x: x["distance"])

        if max_docs is not None:
            docs_with_distance = docs_with_distance[:max_docs]

        return docs_with_distance

    def _determine_optimal_k(self, embeddings: np.ndarray) -> int:
        """
        Determine the optimal number of clusters using silhouette analysis.

        Args:
            embeddings: 2D embedding matrix

        Returns:
            Optimal number of clusters
        """
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score

        n_docs = embeddings.shape[0]

        # Bounds for k
        min_k = 2
        max_k = min(
            self.max_clusters or (n_docs // self.min_cluster_size),
            n_docs - 1,
            20,  # Cap at 20 to keep computation reasonable
        )

        if max_k < min_k:
            return 1 if n_docs == 1 else min_k

        best_k = min_k
        best_score = -1.0

        for k in range(min_k, max_k + 1):
            try:
                kmeans = KMeans(
                    n_clusters=k,
                    random_state=self.random_state,
                    n_init=10,
                    max_iter=300,
                )
                labels = kmeans.fit_predict(embeddings)
                score = silhouette_score(embeddings, labels)

                logger.debug(f"k={k}: silhouette_score={score:.4f}")

                if score > best_score:
                    best_score = score
                    best_k = k
            except Exception as e:
                logger.debug(f"k={k}: failed - {e}")
                continue

        logger.info(f"Selected k={best_k} with silhouette score={best_score:.4f}")
        return best_k

    def _kmeans(self, embeddings: np.ndarray, k: int) -> np.ndarray:
        """
        Run KMeans clustering.

        Args:
            embeddings: 2D embedding matrix
            k: Number of clusters

        Returns:
            Array of cluster labels
        """
        from sklearn.cluster import KMeans

        kmeans = KMeans(
            n_clusters=k,
            random_state=self.random_state,
            n_init=10,
            max_iter=300,
        )
        labels = kmeans.fit_predict(embeddings)

        logger.info(f"KMeans clustering complete: {k} clusters assigned")
        return labels

    def _filter_small_clusters(self, labels: np.ndarray) -> np.ndarray:
        """
        Re-label small clusters as noise (-1).

        Args:
            labels: Original cluster labels

        Returns:
            Labels with small clusters marked as -1
        """
        unique_labels, counts = np.unique(labels, return_counts=True)
        small_clusters = unique_labels[counts < self.min_cluster_size]

        if len(small_clusters) > 0:
            filtered_labels = labels.copy()
            for small_label in small_clusters:
                filtered_labels[labels == small_label] = -1

            # Re-number remaining clusters to be contiguous
            remaining_labels = sorted(set(filtered_labels) - {-1})
            label_map = {old: new for new, old in enumerate(remaining_labels)}
            label_map[-1] = -1

            result = np.array([label_map[lbl] for lbl in filtered_labels])

            n_removed = len(small_clusters)
            n_docs_removed = sum(counts[unique_labels == sc] for sc in small_clusters)
            logger.info(
                f"Filtered {n_removed} small clusters "
                f"({n_docs_removed} documents marked as noise)"
            )
            return result

        return labels

    def _build_cluster_result(
        self, embeddings: np.ndarray, labels: np.ndarray
    ) -> ClusterResult:
        """
        Build a ClusterResult from embeddings and labels.

        Args:
            embeddings: 2D embedding matrix
            labels: Cluster labels

        Returns:
            ClusterResult with all computed metadata
        """
        from scipy.spatial import KDTree

        # Build KDTree for nearest-neighbor search
        kdtree = KDTree(embeddings)

        # Compute cluster sizes
        unique_labels = set(labels.tolist())
        cluster_sizes = {}
        for label in unique_labels:
            cluster_sizes[label] = int(np.sum(labels == label))

        # Compute centroids
        centroids = {}
        for label in unique_labels:
            if label >= 0:
                cluster_embeddings = embeddings[labels == label]
                centroids[label] = cluster_embeddings.mean(axis=0)

        num_clusters = len([lbl for lbl in unique_labels if lbl >= 0])

        logger.info(
            f"Clustering complete: {num_clusters} clusters, "
            f"sizes: {', '.join(f'{k}:{v}' for k, v in sorted(cluster_sizes.items()) if k >= 0)}"
        )

        return ClusterResult(
            cluster_labels=labels,
            num_clusters=num_clusters,
            cluster_sizes=cluster_sizes,
            centroids=centroids,
            embeddings=embeddings,
            kdtree=kdtree,
        )
