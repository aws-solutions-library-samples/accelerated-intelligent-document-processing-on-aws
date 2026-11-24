# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for samples-manifest.json generation in IDPPublisher.

Covers the pure (no-AWS) scanning logic:
- top-level sample documents are indexed as "document" entries
- known doc subdirs (w2, review-routing) are indexed as single
  "batch" entries
- unknown / code-sample subdirs are skipped
- non-document files are skipped
- curated overrides win; unknown files get filename-derived names
- missing samples/ dir → no manifest, no error

One test reads the repository's own samples/ instead of a temporary one: the
review routing scenarios are published only while their folder keeps the name the
publisher knows.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from idp_sdk._core.publish import IDPPublisher

REPO_ROOT = Path(__file__).resolve().parents[4]

# The scenario invoices, in the order the publisher lists them (sorted by name).
REVIEW_ROUTING_FILES = [
    "review-routing/routing-scenario-one-problem.pdf",
    "review-routing/routing-scenario-three-problems.pdf",
    "review-routing/routing-scenario-two-problems.pdf",
]


@pytest.fixture
def publisher_in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs("config_library", exist_ok=True)
    pub = IDPPublisher(verbose=False)
    return pub, tmp_path


def _touch(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4 test")


def _read_manifest(tmp_path):
    return json.loads(
        (tmp_path / "config_library" / "samples-manifest.json").read_text(
            encoding="utf-8"
        )
    )


def test_missing_samples_dir_returns_none(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    assert pub.generate_samples_manifest() is None
    assert not (tmp_path / "config_library" / "samples-manifest.json").exists()


def test_indexes_docs_batches_and_skips_noise(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    samples = tmp_path / "samples"
    # Top-level docs
    _touch(samples / "lending_package.pdf")
    _touch(samples / "Nuveen.pdf")
    _touch(samples / "notes.txt")  # non-document → skipped
    # Known batch subdir
    _touch(samples / "w2" / "W2_0.pdf")
    _touch(samples / "w2" / "W2_1.pdf")
    # Unknown subdir → skipped
    _touch(samples / "lambda-hook-inference" / "handler.py")

    manifest = pub.generate_samples_manifest()
    by_id = {s["id"]: s for s in manifest["samples"]}

    assert manifest["schemaVersion"] == "1.0"
    # txt + code subdir excluded; 2 docs + 1 batch indexed.
    assert set(by_id) == {"lending_package", "Nuveen", "w2"}

    # Curated override applied.
    assert by_id["lending_package"]["name"] == "Lending Package"
    assert by_id["lending_package"]["kind"] == "document"
    assert by_id["lending_package"]["s3Key"] == "samples/lending_package.pdf"
    assert by_id["lending_package"]["fileCount"] == 1

    # Unknown file → filename-derived name.
    assert by_id["Nuveen"]["name"] == "Nuveen"

    # Batch subdir counts its docs and uses a trailing-slash key, and lists
    # each inner file's s3Key so the agent can link a representative example.
    assert by_id["w2"]["kind"] == "batch"
    assert by_id["w2"]["fileCount"] == 2
    assert by_id["w2"]["s3Key"] == "samples/w2/"
    assert by_id["w2"]["files"] == ["samples/w2/W2_0.pdf", "samples/w2/W2_1.pdf"]

    # Written to disk.
    assert _read_manifest(tmp_path) == manifest


def test_empty_known_batch_dir_skipped(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    samples = tmp_path / "samples"
    (samples / "w2").mkdir(parents=True)
    _touch(samples / "invoice.pdf")

    manifest = pub.generate_samples_manifest()
    ids = {s["id"] for s in manifest["samples"]}
    assert ids == {"invoice"}  # empty w2/ produced no entry


def test_config_id_association(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    samples = tmp_path / "samples"
    # Curated override carries an explicit configId.
    _touch(samples / "bank-statement-multipage.pdf")
    # Folder-name convention: config_library/unified/<id> exists.
    (tmp_path / "config_library" / "unified" / "my-custom-sample").mkdir(parents=True)
    _touch(samples / "my-custom-sample.pdf")
    # No override, no matching config folder → configId is None.
    _touch(samples / "Nuveen.pdf")

    manifest = pub.generate_samples_manifest()
    by_id = {s["id"]: s for s in manifest["samples"]}

    # Every entry carries the configId key.
    assert all("configId" in s for s in manifest["samples"])
    assert by_id["bank-statement-multipage"]["configId"] == "bank-statement-sample"
    assert by_id["my-custom-sample"]["configId"] == "my-custom-sample"
    assert by_id["Nuveen"]["configId"] is None


def test_batch_config_id_and_file_list(publisher_in_tmp):
    pub, tmp_path = publisher_in_tmp
    samples = tmp_path / "samples"
    _touch(samples / "lending_package.pdf")
    _touch(samples / "w2" / "W2_0.pdf")
    _touch(samples / "w2" / "W2_1.pdf")
    _touch(samples / "notes.txt")  # non-document → excluded from file list

    manifest = pub.generate_samples_manifest()
    by_id = {s["id"]: s for s in manifest["samples"]}
    # w2 batch maps to the fake-w2 preset via the overrides table.
    assert by_id["w2"]["configId"] == "fake-w2"

    # The deploy-time copy file list matches the curated document files
    # (relative to samples/), excluding non-document files.
    assert pub.generate_sample_file_list() == [
        "lending_package.pdf",
        "w2/W2_0.pdf",
        "w2/W2_1.pdf",
    ]


def test_review_routing_scenarios_are_one_batch_for_the_showcase_preset(
    publisher_in_tmp,
):
    pub, tmp_path = publisher_in_tmp
    samples = tmp_path / "samples"
    for relative in REVIEW_ROUTING_FILES:
        _touch(samples / relative)
    # The truth file beside the invoices is not a document → not published.
    (samples / "review-routing" / "scenarios.truth.json").write_text("{}")

    manifest = pub.generate_samples_manifest()

    assert manifest["samples"] == [
        {
            "id": "review-routing",
            "name": "Review routing scenarios",
            "description": (
                "Three synthetic invoices with one, two and three problems; each "
                "returns to review once per remaining problem."
            ),
            "s3Key": "samples/review-routing/",
            "files": [f"samples/{relative}" for relative in REVIEW_ROUTING_FILES],
            "kind": "batch",
            "fileCount": 3,
            "configId": "review-routing-showcase",
        }
    ]
    assert pub.generate_sample_file_list() == REVIEW_ROUTING_FILES


def test_the_repository_ships_the_review_routing_scenarios(monkeypatch):
    """Against the real tree: the folder name, the files and the preset line up.

    A folder under another name would be skipped like a code sample, and a
    configId naming no preset would be offered by the UI and not found. Neither
    shows in a test that builds its own samples/.
    """
    monkeypatch.chdir(REPO_ROOT)
    pub = IDPPublisher(verbose=False)

    shipped = [f for f in pub.iter_sample_files() if f.startswith("review-routing/")]
    assert shipped == REVIEW_ROUTING_FILES

    _name, _description, config_id = pub._sample_label("review-routing")
    assert config_id == "review-routing-showcase"
    assert (REPO_ROOT / "config_library" / "unified" / config_id).is_dir()


def test_the_policy_document_of_rule_validation_is_not_a_sample_to_process(
    monkeypatch,
):
    """Against the real tree: samples/rule-validation holds the policy document
    that policy rules are extracted from. Listed as a sample, it would be offered
    as a document to process under the rule-validation configuration. The document
    that configuration validates is the bank statement, which is listed."""
    monkeypatch.chdir(REPO_ROOT)
    pub = IDPPublisher(verbose=False)

    assert (REPO_ROOT / "samples" / "rule-validation").is_dir()
    assert any((REPO_ROOT / "samples" / "rule-validation").glob("*.pdf"))
    shipped = list(pub.iter_sample_files())
    assert not [f for f in shipped if f.startswith("rule-validation")]
    assert "bank-statement-multipage.pdf" in shipped
