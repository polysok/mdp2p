"""Tests for the reviewer-mode fields added to ClientConfig."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mdp2p_client.config import ClientConfig


class TestReviewerFields:
    def test_defaults_off(self):
        cfg = ClientConfig(author="alice")
        assert cfg.reviewer_mode is False
        assert cfg.reviewer_categories == []
        assert cfg.reviewer_dir.endswith("reviewer")

    def test_save_and_load_roundtrip(self, tmp_path):
        path = tmp_path / "config.json"
        cfg = ClientConfig(
            author="alice",
            reviewer_mode=True,
            reviewer_dir=str(tmp_path / "rev"),
            reviewer_categories=["computing", "languages_linguistics"],
        )
        cfg.save(path)
        loaded = ClientConfig.load(path)
        assert loaded is not None
        assert loaded.reviewer_mode is True
        assert loaded.reviewer_categories == ["computing", "languages_linguistics"]
        assert loaded.reviewer_dir == str(tmp_path / "rev")

    def test_legacy_config_without_reviewer_fields_loads_with_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps({"author": "bob", "port": 1234}), encoding="utf-8"
        )
        loaded = ClientConfig.load(path)
        assert loaded is not None
        assert loaded.author == "bob"
        assert loaded.port == 1234
        assert loaded.reviewer_mode is False
        assert loaded.reviewer_categories == []

    def test_unknown_future_fields_dropped_silently(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(
            json.dumps(
                {
                    "author": "carol",
                    "reviewer_mode": True,
                    "future_unknown_field": "keep-calm",
                }
            ),
            encoding="utf-8",
        )
        loaded = ClientConfig.load(path)
        assert loaded is not None
        assert loaded.reviewer_mode is True
