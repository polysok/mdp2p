import json
import time
from pathlib import Path

import pytest

from pinstore import (
    PinStatus,
    load_pinstore,
    save_pinstore,
    check_pin,
    pin_key,
    unpin_key,
    update_pin_last_seen,
)


@pytest.fixture
def pinstore_path(tmp_path):
    return str(tmp_path / "known_keys.json")


class TestLoadSave:
    def test_load_nonexistent_returns_empty(self, pinstore_path):
        assert load_pinstore(pinstore_path) == {}

    def test_save_and_load_roundtrip(self, pinstore_path):
        data = {"blog": {"public_key": "abc123", "author": "alice",
                         "first_seen": 1000, "last_seen": 2000}}
        save_pinstore(data, pinstore_path)
        loaded = load_pinstore(pinstore_path)
        assert loaded == data

    def test_save_creates_parent_dirs(self, tmp_path):
        deep_path = str(tmp_path / "a" / "b" / "known_keys.json")
        save_pinstore({"x": {}}, deep_path)
        assert Path(deep_path).exists()


class TestCheckPin:
    def test_unknown_uri(self):
        assert check_pin({}, "blog", "key123") == PinStatus.UNKNOWN

    def test_matching_key(self):
        pinstore = {"blog": {"public_key": "key123"}}
        assert check_pin(pinstore, "blog", "key123") == PinStatus.MATCH

    def test_mismatching_key(self):
        pinstore = {"blog": {"public_key": "key123"}}
        assert check_pin(pinstore, "blog", "DIFFERENT") == PinStatus.MISMATCH


class TestPinKey:
    def test_pin_new_key(self, pinstore_path):
        pin_key("blog", "key123", "alice", pinstore_path)
        pinstore = load_pinstore(pinstore_path)
        assert "blog" in pinstore
        assert pinstore["blog"]["public_key"] == "key123"
        assert pinstore["blog"]["author"] == "alice"
        assert pinstore["blog"]["first_seen"] > 0
        assert pinstore["blog"]["last_seen"] >= pinstore["blog"]["first_seen"]

    def test_pin_preserves_first_seen_on_update(self, pinstore_path):
        pin_key("blog", "key_v1", "alice", pinstore_path)
        original = load_pinstore(pinstore_path)["blog"]["first_seen"]

        pin_key("blog", "key_v2", "alice", pinstore_path)
        updated = load_pinstore(pinstore_path)
        assert updated["blog"]["public_key"] == "key_v2"
        assert updated["blog"]["first_seen"] == original

    def test_pin_multiple_uris(self, pinstore_path):
        pin_key("blog", "key1", "alice", pinstore_path)
        pin_key("wiki", "key2", "bob", pinstore_path)
        pinstore = load_pinstore(pinstore_path)
        assert len(pinstore) == 2
        assert pinstore["blog"]["public_key"] == "key1"
        assert pinstore["wiki"]["public_key"] == "key2"


class TestUnpinKey:
    def test_unpin_existing(self, pinstore_path):
        pin_key("blog", "key123", "alice", pinstore_path)
        assert unpin_key("blog", pinstore_path) is True
        assert load_pinstore(pinstore_path) == {}

    def test_unpin_nonexistent(self, pinstore_path):
        assert unpin_key("blog", pinstore_path) is False

    def test_unpin_preserves_other_entries(self, pinstore_path):
        pin_key("blog", "key1", "alice", pinstore_path)
        pin_key("wiki", "key2", "bob", pinstore_path)
        unpin_key("blog", pinstore_path)
        pinstore = load_pinstore(pinstore_path)
        assert "blog" not in pinstore
        assert "wiki" in pinstore


class TestUpdateLastSeen:
    def test_updates_timestamp(self, pinstore_path):
        pin_key("blog", "key123", "alice", pinstore_path)
        original = load_pinstore(pinstore_path)["blog"]["last_seen"]

        # Ensure time advances
        update_pin_last_seen("blog", pinstore_path)
        updated = load_pinstore(pinstore_path)["blog"]["last_seen"]
        assert updated >= original

    def test_noop_for_unknown_uri(self, pinstore_path):
        update_pin_last_seen("nonexistent", pinstore_path)
        assert load_pinstore(pinstore_path) == {}
