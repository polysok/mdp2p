import json
from pathlib import Path

import pytest

from trust.store import (
    get_peer,
    load_store,
    record_confirmed,
    record_disputed,
    save_store,
    set_explicit_trust,
)


@pytest.fixture
def store_path(tmp_path):
    return str(tmp_path / "trust.json")


class TestLoadSave:
    def test_load_nonexistent_returns_empty_structure(self, store_path):
        assert load_store(store_path) == {"peers": {}}

    def test_save_and_load_roundtrip(self, store_path):
        data = {
            "peers": {
                "alice_pubkey": {
                    "explicit_trust": 0.8,
                    "confirmed_signals": 3,
                    "disputed_signals": 1,
                    "last_seen": 1000,
                }
            }
        }
        save_store(data, store_path)
        assert load_store(store_path) == data

    def test_save_creates_parent_dirs(self, tmp_path):
        deep_path = str(tmp_path / "a" / "b" / "trust.json")
        save_store({"peers": {}}, deep_path)
        assert Path(deep_path).exists()

    def test_load_repairs_missing_peers_key(self, store_path):
        Path(store_path).write_text(json.dumps({}), encoding="utf-8")
        assert load_store(store_path) == {"peers": {}}


class TestGetPeer:
    def test_returns_neutral_defaults_for_unknown(self):
        store = {"peers": {}}
        record = get_peer(store, "unknown_key")
        assert record == {
            "explicit_trust": None,
            "confirmed_signals": 0,
            "disputed_signals": 0,
            "last_seen": 0,
        }

    def test_persists_new_peer_in_store(self):
        store = {"peers": {}}
        get_peer(store, "new_key")
        assert "new_key" in store["peers"]

    def test_returns_existing_record(self):
        store = {"peers": {"k": {"confirmed_signals": 5}}}
        assert get_peer(store, "k")["confirmed_signals"] == 5


class TestRecordConfirmed:
    def test_increments_counter(self):
        store = {"peers": {}}
        record_confirmed(store, "k", now=100)
        record_confirmed(store, "k", now=200)
        assert store["peers"]["k"]["confirmed_signals"] == 2
        assert store["peers"]["k"]["last_seen"] == 200

    def test_does_not_touch_other_peers(self):
        store = {"peers": {"a": {"confirmed_signals": 0}}}
        record_confirmed(store, "b", now=100)
        assert store["peers"]["a"]["confirmed_signals"] == 0


class TestRecordDisputed:
    def test_increments_counter(self):
        store = {"peers": {}}
        record_disputed(store, "k", now=100)
        record_disputed(store, "k", now=200)
        assert store["peers"]["k"]["disputed_signals"] == 2
        assert store["peers"]["k"]["last_seen"] == 200

    def test_confirmed_and_disputed_are_independent(self):
        store = {"peers": {}}
        record_confirmed(store, "k", now=100)
        record_disputed(store, "k", now=200)
        assert store["peers"]["k"]["confirmed_signals"] == 1
        assert store["peers"]["k"]["disputed_signals"] == 1


class TestSetExplicitTrust:
    def test_sets_numeric_value(self):
        store = {"peers": {}}
        set_explicit_trust(store, "k", 1.5)
        assert store["peers"]["k"]["explicit_trust"] == 1.5

    def test_clears_with_none(self):
        store = {"peers": {"k": {"explicit_trust": 0.8}}}
        set_explicit_trust(store, "k", None)
        assert store["peers"]["k"]["explicit_trust"] is None

    def test_creates_peer_if_missing(self):
        store = {"peers": {}}
        set_explicit_trust(store, "new", 0.5)
        assert store["peers"]["new"]["explicit_trust"] == 0.5
        assert store["peers"]["new"]["confirmed_signals"] == 0
