"""Unit tests for alpha_engine.research.manifest (ADR-031)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.research.manifest import (  # noqa: E402
    ADR_VERSION,
    DatasetSnapshot,
    ResearchManifest,
    sha256_bytes,
    sha256_obj,
)


class TestSha256Helpers:
    def test_sha256_bytes_is_deterministic(self):
        data = b"hello world"
        assert sha256_bytes(data) == sha256_bytes(data)

    def test_sha256_bytes_hex_length_is_64(self):
        assert len(sha256_bytes(b"x")) == 64

    def test_sha256_bytes_different_input_differs(self):
        assert sha256_bytes(b"a") != sha256_bytes(b"b")

    def test_sha256_obj_is_deterministic(self):
        obj = {"b": 2, "a": 1}
        assert sha256_obj(obj) == sha256_obj(obj)

    def test_sha256_obj_ignores_key_order(self):
        assert sha256_obj({"a": 1, "b": 2}) == sha256_obj({"b": 2, "a": 1})

    def test_sha256_obj_different_values_differ(self):
        assert sha256_obj({"a": 1}) != sha256_obj({"a": 2})

    def test_sha256_obj_empty_dict_stable(self):
        h1 = sha256_obj({})
        h2 = sha256_obj({})
        assert h1 == h2


class TestDatasetSnapshot:
    def test_defaults_are_safe_sentinels(self):
        ds = DatasetSnapshot()
        assert ds.dataset_id == "unspecified"
        assert ds.universe_definition_version == "ADR-019"
        assert ds.corporate_action_snapshot == "none"
        assert ds.bhavcopy_snapshot == "none"

    def test_custom_values_persist(self):
        ds = DatasetSnapshot(dataset_id="ds-001", dataset_hash="abc123")
        assert ds.dataset_id == "ds-001"
        assert ds.dataset_hash == "abc123"

    def test_frozen_immutability(self):
        ds = DatasetSnapshot()
        with pytest.raises((AttributeError, TypeError)):
            ds.dataset_id = "changed"  # type: ignore[misc]


class TestResearchManifestBuild:
    def test_adr_version_matches_module_constant(self):
        m = ResearchManifest.build()
        assert m.adr_version == ADR_VERSION

    def test_generated_at_is_utc_iso_string(self):
        m = ResearchManifest.build()
        assert "+00:00" in m.generated_at

    def test_git_commit_is_non_empty_string(self):
        m = ResearchManifest.build()
        assert isinstance(m.git_commit, str)
        assert len(m.git_commit) > 0

    def test_different_configs_produce_different_config_hashes(self):
        m1 = ResearchManifest.build(config={"key": "v1"})
        m2 = ResearchManifest.build(config={"key": "v2"})
        assert m1.config_hash != m2.config_hash

    def test_none_and_empty_config_produce_same_hash(self):
        m1 = ResearchManifest.build(config=None)
        m2 = ResearchManifest.build(config={})
        assert m1.config_hash == m2.config_hash

    def test_model_versions_stored(self):
        m = ResearchManifest.build(model_versions=["v1.0", "v1.1"])
        assert m.model_versions == ["v1.0", "v1.1"]

    def test_horizons_stored(self):
        m = ResearchManifest.build(horizons=[15, 30, 60])
        assert m.horizons == [15, 30, 60]

    def test_input_bytes_are_sha256_hashed(self):
        m = ResearchManifest.build(inputs={"prices": b"raw data"})
        assert "prices" in m.inputs
        assert len(m.inputs["prices"]) == 64

    def test_input_hash_matches_sha256_bytes(self):
        data = b"raw data"
        m = ResearchManifest.build(inputs={"prices": data})
        assert m.inputs["prices"] == sha256_bytes(data)

    def test_dataset_passed_through(self):
        ds = DatasetSnapshot(dataset_id="ds-001", dataset_hash="aaa")
        m = ResearchManifest.build(dataset=ds)
        assert m.dataset.dataset_id == "ds-001"

    def test_default_dataset_is_snapshot_with_sentinel_values(self):
        m = ResearchManifest.build()
        assert m.dataset.dataset_id == "unspecified"


class TestResearchManifestToDict:
    def test_to_dict_is_json_serializable(self):
        m = ResearchManifest.build(model_versions=["v1"], horizons=[15])
        d = m.to_dict()
        json.dumps(d, default=str)

    def test_to_dict_contains_required_keys(self):
        m = ResearchManifest.build()
        d = m.to_dict()
        for key in ("generated_at", "git_commit", "adr_version", "config_hash",
                    "model_versions", "horizons", "dataset", "inputs"):
            assert key in d

    def test_to_dict_dataset_is_dict(self):
        m = ResearchManifest.build()
        d = m.to_dict()
        assert isinstance(d["dataset"], dict)


class TestResearchManifestWrite:
    def test_write_creates_file_at_given_path(self):
        m = ResearchManifest.build()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            result = m.write(path)
            assert result == path
            assert path.exists()

    def test_write_produces_valid_json(self):
        m = ResearchManifest.build()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            m.write(path)
            data = json.loads(path.read_text())
            assert data["adr_version"] == ADR_VERSION

    def test_write_returns_path_object(self):
        m = ResearchManifest.build()
        with tempfile.TemporaryDirectory() as tmp:
            result = m.write(str(Path(tmp) / "out.json"))
            assert isinstance(result, Path)


class TestResearchManifestSummaryMd:
    def test_contains_git_commit_label(self):
        m = ResearchManifest.build()
        md = m.summary_md()
        assert "git_commit" in md

    def test_contains_adr_version_value(self):
        m = ResearchManifest.build()
        md = m.summary_md()
        assert ADR_VERSION in md

    def test_contains_config_hash_prefix(self):
        m = ResearchManifest.build(config={"x": 1})
        md = m.summary_md()
        assert m.config_hash[:12] in md

    def test_contains_dataset_id(self):
        ds = DatasetSnapshot(dataset_id="ds-test-42")
        m = ResearchManifest.build(dataset=ds)
        md = m.summary_md()
        assert "ds-test-42" in md

    def test_output_is_multiline(self):
        m = ResearchManifest.build()
        md = m.summary_md()
        assert "\n" in md
