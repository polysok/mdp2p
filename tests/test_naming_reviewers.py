"""Tests for the reviewer registry side of the naming server."""

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

from bundle import public_key_to_b64
from naming import (
    NameStore,
    NamingServer,
    ReviewerStore,
    client_list_reviewers,
    client_register_reviewer,
)
from review import (
    build_reviewer_opt_in,
    sign_reviewer_opt_in,
    verify_reviewer_opt_in,
)


PEER_ID = "12D3KooWTestPeerIdForNamingReviewerTests01"
ADDRS = ["/ip4/127.0.0.1/tcp/4001"]


def _fresh_host():
    return new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))


def _make_reviewer() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    return priv, public_key_to_b64(priv.public_key())


@asynccontextmanager
async def env(
    tmp_path: Path,
    with_reviewer_store: bool = True,
):
    """Spawn a naming server (optionally without a reviewer store) + client host."""
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

        store = NameStore(str(tmp_path / "names.json"))
        reviewer_store = (
            ReviewerStore(str(tmp_path / "reviewers.json"))
            if with_reviewer_store
            else None
        )
        server = NamingServer(server_host, store, reviewer_store)
        server.attach()

        server_maddr = (
            f"/ip4/127.0.0.1/tcp/{server_port}/p2p/{server_host.get_id().to_string()}"
        )
        server_info = info_from_p2p_addr(multiaddr.Multiaddr(server_maddr))

        try:
            yield client_host, server_info, reviewer_store
        finally:
            nursery.cancel_scope.cancel()


def _run(coro_factory):
    trio.run(coro_factory)


# ─── Happy path ─────────────────────────────────────────────────────────


def test_register_and_list_reviewer(tmp_path):
    priv, pub_b64 = _make_reviewer()
    record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, categories=["tech"])
    signature = sign_reviewer_opt_in(record, priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _store):
            resp = await client_register_reviewer(client, server_info, record, signature)
            assert resp["type"] == "ok"
            assert resp["public_key"] == pub_b64

            listing = await client_list_reviewers(client, server_info)
            assert listing["type"] == "reviewers"
            assert len(listing["records"]) == 1
            entry = listing["records"][0]
            assert entry["record"] == record
            assert entry["signature"] == signature

    _run(main)


def test_list_reviewers_empty_registry(tmp_path):
    async def main():
        async with env(tmp_path) as (client, server_info, _store):
            listing = await client_list_reviewers(client, server_info)
            assert listing["type"] == "reviewers"
            assert listing["records"] == []

    _run(main)


def test_multiple_reviewers_registered(tmp_path):
    reviewers = [_make_reviewer() for _ in range(3)]

    async def main():
        async with env(tmp_path) as (client, server_info, _store):
            for priv, pub in reviewers:
                record = build_reviewer_opt_in(pub, PEER_ID, ADDRS)
                sig = sign_reviewer_opt_in(record, priv)
                resp = await client_register_reviewer(client, server_info, record, sig)
                assert resp["type"] == "ok"

            listing = await client_list_reviewers(client, server_info)
            pubkeys = {e["record"]["public_key"] for e in listing["records"]}
            assert pubkeys == {pub for _, pub in reviewers}

    _run(main)


def test_listed_entries_pass_local_verification(tmp_path):
    priv, pub_b64 = _make_reviewer()
    record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, categories=["fr"])
    signature = sign_reviewer_opt_in(record, priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _store):
            await client_register_reviewer(client, server_info, record, signature)
            listing = await client_list_reviewers(client, server_info)

            for entry in listing["records"]:
                ok, err = verify_reviewer_opt_in(entry["record"], entry["signature"])
                assert ok, err

    _run(main)


# ─── Security / validation ─────────────────────────────────────────────


def test_tampered_signature_rejected(tmp_path):
    priv, pub_b64 = _make_reviewer()
    record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, categories=["tech"])
    signature = sign_reviewer_opt_in(record, priv)
    record["categories"] = ["politics"]  # post-sign tamper

    async def main():
        async with env(tmp_path) as (client, server_info, _store):
            resp = await client_register_reviewer(client, server_info, record, signature)
            assert resp["type"] == "error"
            assert "invalid signature" in resp["msg"]

    _run(main)


def test_stale_timestamp_rejected(tmp_path):
    priv, pub_b64 = _make_reviewer()
    now = int(time.time())
    first = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, timestamp=now)
    sig_first = sign_reviewer_opt_in(first, priv)
    stale = build_reviewer_opt_in(
        pub_b64, PEER_ID, ADDRS, categories=["new"], timestamp=now
    )
    sig_stale = sign_reviewer_opt_in(stale, priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _store):
            resp = await client_register_reviewer(client, server_info, first, sig_first)
            assert resp["type"] == "ok"

            resp = await client_register_reviewer(client, server_info, stale, sig_stale)
            assert resp["type"] == "error"
            assert "timestamp" in resp["msg"].lower()

    _run(main)


def test_newer_update_accepted(tmp_path):
    priv, pub_b64 = _make_reviewer()
    now = int(time.time())
    v1 = build_reviewer_opt_in(
        pub_b64, PEER_ID, ADDRS, categories=["tech"], timestamp=now
    )
    sig_v1 = sign_reviewer_opt_in(v1, priv)
    v2 = build_reviewer_opt_in(
        pub_b64,
        PEER_ID,
        ["/ip4/127.0.0.1/tcp/4002"],  # reviewer moved addresses
        categories=["tech", "fr"],
        timestamp=now + 1,
    )
    sig_v2 = sign_reviewer_opt_in(v2, priv)

    async def main():
        async with env(tmp_path) as (client, server_info, _store):
            resp = await client_register_reviewer(client, server_info, v1, sig_v1)
            assert resp["type"] == "ok"

            resp = await client_register_reviewer(client, server_info, v2, sig_v2)
            assert resp["type"] == "ok"

            listing = await client_list_reviewers(client, server_info)
            records = listing["records"]
            assert len(records) == 1
            assert records[0]["record"]["categories"] == ["tech", "fr"]

    _run(main)


# ─── Registry disabled ─────────────────────────────────────────────────


def test_register_reviewer_errors_without_registry(tmp_path):
    priv, pub_b64 = _make_reviewer()
    record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
    signature = sign_reviewer_opt_in(record, priv)

    async def main():
        async with env(tmp_path, with_reviewer_store=False) as (
            client,
            server_info,
            _store,
        ):
            resp = await client_register_reviewer(client, server_info, record, signature)
            assert resp["type"] == "error"
            assert "reviewer registry disabled" in resp["msg"]

    _run(main)


def test_list_reviewers_returns_empty_without_registry(tmp_path):
    async def main():
        async with env(tmp_path, with_reviewer_store=False) as (
            client,
            server_info,
            _store,
        ):
            listing = await client_list_reviewers(client, server_info)
            assert listing["type"] == "reviewers"
            assert listing["records"] == []

    _run(main)


# ─── Persistence ───────────────────────────────────────────────────────


def test_reviewer_registry_survives_restart(tmp_path):
    priv, pub_b64 = _make_reviewer()
    record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, categories=["tech"])
    signature = sign_reviewer_opt_in(record, priv)

    async def write():
        async with env(tmp_path) as (client, server_info, _store):
            resp = await client_register_reviewer(client, server_info, record, signature)
            assert resp["type"] == "ok"

    _run(write)

    reviewer_file = tmp_path / "reviewers.json"
    assert reviewer_file.exists()
    raw = json.loads(reviewer_file.read_text())
    assert pub_b64 in raw

    async def read_back():
        async with env(tmp_path) as (client, server_info, store):
            listing = await client_list_reviewers(client, server_info)
            assert len(listing["records"]) == 1
            assert listing["records"][0]["record"]["public_key"] == pub_b64
            # In-memory store picked up the on-disk record.
            assert store is not None
            assert len(store.list_records()) == 1

    _run(read_back)
