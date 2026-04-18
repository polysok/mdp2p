"""Tests for the naming module (libp2p + trio)."""

from contextlib import asynccontextmanager
import json
import secrets
import sys
import time
from pathlib import Path

import multiaddr
import pytest
import trio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.utils.address_validation import find_free_port

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bundle import (
    build_name_record,
    public_key_to_b64,
    sign_name_record,
)
from naming import (
    NameStore,
    NamingServer,
    client_list,
    client_register,
    client_resolve,
)


def _fresh_host():
    return new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))


def _make_author() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    return priv, public_key_to_b64(priv.public_key())


@asynccontextmanager
async def naming_env(store_path: Path):
    """Spawn a naming server + a client host on localhost. Tear down on exit."""
    server_host = _fresh_host()
    client_host = _fresh_host()
    server_port = find_free_port()
    client_port = find_free_port()

    async with (
        server_host.run(
            listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{server_port}")]
        ),
        client_host.run(
            listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{client_port}")]
        ),
        trio.open_nursery() as nursery,
    ):
        nursery.start_soon(server_host.get_peerstore().start_cleanup_task, 60)
        nursery.start_soon(client_host.get_peerstore().start_cleanup_task, 60)

        store = NameStore(str(store_path))
        server = NamingServer(server_host, store)
        server.attach()

        server_maddr = (
            f"/ip4/127.0.0.1/tcp/{server_port}/p2p/{server_host.get_id().to_string()}"
        )
        server_info = info_from_p2p_addr(multiaddr.Multiaddr(server_maddr))

        try:
            yield client_host, server_info, store
        finally:
            nursery.cancel_scope.cancel()


def _run(coro_factory):
    """Run an async test function under trio."""
    trio.run(coro_factory)


# ─── Happy path ─────────────────────────────────────────────────────────

def test_register_and_resolve(tmp_path):
    priv, pub_b64 = _make_author()
    record = build_name_record("blog.alice", "alice", pub_b64, "a" * 64)
    signature = sign_name_record(record, priv)

    async def main():
        async with naming_env(tmp_path / "store.json") as (client, server_info, _store):
            ok = await client_register(client, server_info, record, signature)
            assert ok["type"] == "ok"
            assert ok["uri"] == "blog.alice"

            resolved = await client_resolve(client, server_info, "blog.alice")
            assert resolved["type"] == "record"
            assert resolved["record"] == record
            assert resolved["signature"] == signature

    _run(main)


def test_resolve_unknown_uri_returns_error(tmp_path):
    async def main():
        async with naming_env(tmp_path / "store.json") as (client, server_info, _store):
            resp = await client_resolve(client, server_info, "nothing.here")
            assert resp["type"] == "error"
            assert "unknown" in resp["msg"].lower()

    _run(main)


def test_list_returns_all_records(tmp_path):
    priv, pub_b64 = _make_author()

    async def main():
        async with naming_env(tmp_path / "store.json") as (client, server_info, _store):
            for uri in ("site.one", "site.two", "site.three"):
                record = build_name_record(uri, "alice", pub_b64, "0" * 64)
                signature = sign_name_record(record, priv)
                resp = await client_register(client, server_info, record, signature)
                assert resp["type"] == "ok"

            listing = await client_list(client, server_info)
            assert listing["type"] == "names"
            uris = {r["uri"] for r in listing["records"]}
            assert uris == {"site.one", "site.two", "site.three"}

    _run(main)


# ─── Security / validation ─────────────────────────────────────────────

def test_tampered_signature_rejected(tmp_path):
    priv, pub_b64 = _make_author()
    record = build_name_record("blog.alice", "alice", pub_b64, "a" * 64)
    signature = sign_name_record(record, priv)

    tampered = dict(record)
    tampered["manifest_ref"] = "b" * 64  # post-sign modification

    async def main():
        async with naming_env(tmp_path / "store.json") as (client, server_info, _store):
            resp = await client_register(client, server_info, tampered, signature)
            assert resp["type"] == "error"
            assert "invalid signature" in resp["msg"]

    _run(main)


def test_different_pubkey_same_uri_rejected(tmp_path):
    priv_a, pub_a = _make_author()
    priv_b, pub_b = _make_author()

    record_a = build_name_record("blog.alice", "alice", pub_a, "a" * 64)
    sig_a = sign_name_record(record_a, priv_a)
    record_b = build_name_record("blog.alice", "mallory", pub_b, "a" * 64)
    sig_b = sign_name_record(record_b, priv_b)

    async def main():
        async with naming_env(tmp_path / "store.json") as (client, server_info, _store):
            resp = await client_register(client, server_info, record_a, sig_a)
            assert resp["type"] == "ok"

            hijack = await client_register(client, server_info, record_b, sig_b)
            assert hijack["type"] == "error"
            assert "different public key" in hijack["msg"]

    _run(main)


def test_stale_timestamp_rejected(tmp_path):
    priv, pub_b64 = _make_author()
    now = int(time.time())
    first = build_name_record("blog.alice", "alice", pub_b64, "a" * 64, timestamp=now)
    sig_first = sign_name_record(first, priv)
    # Same timestamp, different content — must be refused (no replay).
    stale = build_name_record("blog.alice", "alice", pub_b64, "b" * 64, timestamp=now)
    sig_stale = sign_name_record(stale, priv)

    async def main():
        async with naming_env(tmp_path / "store.json") as (client, server_info, _store):
            resp = await client_register(client, server_info, first, sig_first)
            assert resp["type"] == "ok"

            resp = await client_register(client, server_info, stale, sig_stale)
            assert resp["type"] == "error"
            assert "timestamp" in resp["msg"].lower()

    _run(main)


def test_newer_update_accepted(tmp_path):
    priv, pub_b64 = _make_author()
    now = int(time.time())
    v1 = build_name_record("blog.alice", "alice", pub_b64, "a" * 64, timestamp=now)
    sig_v1 = sign_name_record(v1, priv)
    v2 = build_name_record("blog.alice", "alice", pub_b64, "c" * 64, timestamp=now + 1)
    sig_v2 = sign_name_record(v2, priv)

    async def main():
        async with naming_env(tmp_path / "store.json") as (client, server_info, _store):
            resp = await client_register(client, server_info, v1, sig_v1)
            assert resp["type"] == "ok"

            resp = await client_register(client, server_info, v2, sig_v2)
            assert resp["type"] == "ok"

            resolved = await client_resolve(client, server_info, "blog.alice")
            assert resolved["record"]["manifest_ref"] == "c" * 64

    _run(main)


def test_invalid_uri_rejected_on_register(tmp_path):
    priv, pub_b64 = _make_author()
    # URI with a forbidden slash; sign-able but should be refused by the server.
    record = build_name_record("evil/uri", "alice", pub_b64, "a" * 64)
    signature = sign_name_record(record, priv)

    async def main():
        async with naming_env(tmp_path / "store.json") as (client, server_info, _store):
            resp = await client_register(client, server_info, record, signature)
            assert resp["type"] == "error"
            assert "forbidden" in resp["msg"].lower() or "invalid" in resp["msg"].lower()

    _run(main)


# ─── Persistence ────────────────────────────────────────────────────────

def test_records_survive_restart(tmp_path):
    priv, pub_b64 = _make_author()
    record = build_name_record("blog.alice", "alice", pub_b64, "a" * 64)
    signature = sign_name_record(record, priv)
    store_path = tmp_path / "store.json"

    async def write():
        async with naming_env(store_path) as (client, server_info, _store):
            resp = await client_register(client, server_info, record, signature)
            assert resp["type"] == "ok"

    _run(write)

    # Sanity: the file on disk contains the record.
    assert store_path.exists()
    raw = json.loads(store_path.read_text())
    assert "blog.alice" in raw
    assert raw["blog.alice"]["record"]["manifest_ref"] == "a" * 64

    async def read_back():
        async with naming_env(store_path) as (client, server_info, store):
            assert {r["uri"] for r in store.list_records()} == {"blog.alice"}
            resolved = await client_resolve(client, server_info, "blog.alice")
            assert resolved["type"] == "record"
            assert resolved["record"]["manifest_ref"] == "a" * 64

    _run(read_back)
