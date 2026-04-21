"""Unit + integration tests for peer.py review-solicitation helpers."""

from contextlib import asynccontextmanager
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
    AssignmentStore,
    AttachmentStore,
    NameStore,
    NamingServer,
    ReviewerStore,
    client_list_assignments,
    client_register_reviewer,
)
from peer.peer import Peer, _extract_fresh_reviewer_pool
from review import build_reviewer_opt_in, sign_reviewer_opt_in


PEER_ID = "12D3KooWExtractFreshReviewerTestPeerId1111"
ADDRS = ["/ip4/127.0.0.1/tcp/4001"]


def _signed_entry(timestamp: int) -> dict:
    priv = Ed25519PrivateKey.generate()
    pub_b64 = public_key_to_b64(priv.public_key())
    record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS, timestamp=timestamp)
    signature = sign_reviewer_opt_in(record, priv)
    return {"record": record, "signature": signature}, pub_b64


class TestExtractFreshReviewerPool:
    def test_empty_input_returns_empty(self):
        assert _extract_fresh_reviewer_pool([], None) == []

    def test_valid_entries_returned_as_pubkeys(self):
        now = int(time.time())
        e1, pub1 = _signed_entry(now)
        e2, pub2 = _signed_entry(now)
        pool = _extract_fresh_reviewer_pool([e1, e2], None)
        assert sorted(pool) == sorted([pub1, pub2])

    def test_invalid_signature_dropped(self):
        now = int(time.time())
        entry, _ = _signed_entry(now)
        entry["record"]["categories"] = ["tampered"]  # post-sign tamper
        pool = _extract_fresh_reviewer_pool([entry], None)
        assert pool == []

    def test_freshness_filter_drops_stale(self):
        now = int(time.time())
        fresh_entry, fresh_pub = _signed_entry(now)
        stale_entry, _ = _signed_entry(now - 3600)  # one hour ago
        pool = _extract_fresh_reviewer_pool(
            [fresh_entry, stale_entry], freshness_seconds=600
        )
        assert pool == [fresh_pub]

    def test_no_freshness_keeps_all_valid(self):
        now = int(time.time())
        fresh_entry, _ = _signed_entry(now)
        stale_entry, _ = _signed_entry(now - 365 * 86400)
        pool = _extract_fresh_reviewer_pool(
            [fresh_entry, stale_entry], freshness_seconds=None
        )
        assert len(pool) == 2

    def test_malformed_entries_ignored(self):
        now = int(time.time())
        valid, pub = _signed_entry(now)
        pool = _extract_fresh_reviewer_pool(
            [
                None,
                {},
                {"record": "not-a-dict"},
                {"record": {}, "signature": ""},
                valid,
            ],
            None,
        )
        assert pool == [pub]


# ─── Integration: _solicit_reviews against a real naming server ─────────


def _fresh_host():
    return new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))


@asynccontextmanager
async def solicit_env(tmp_path: Path):
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
        reviewer_store = ReviewerStore(str(tmp_path / "reviewers.json"))
        assignment_store = AssignmentStore(str(tmp_path / "assignments.json"))
        attachment_store = AttachmentStore(str(tmp_path / "attachments.json"))
        server = NamingServer(
            server_host, store, reviewer_store, assignment_store, attachment_store
        )
        server.attach()

        server_maddr = (
            f"/ip4/127.0.0.1/tcp/{server_port}/p2p/{server_host.get_id().to_string()}"
        )
        server_info = info_from_p2p_addr(multiaddr.Multiaddr(server_maddr))

        peer = Peer(
            host=client_host,
            data_dir=str(tmp_path / "peer_data"),
            naming_info=server_info,
            pinstore_path=str(tmp_path / "pins.json"),
        )

        try:
            yield peer, client_host, server_info
        finally:
            nursery.cancel_scope.cancel()


def _run(coro_factory):
    trio.run(coro_factory)


async def _register_reviewer(client, server_info) -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    pub_b64 = public_key_to_b64(priv.public_key())
    record = build_reviewer_opt_in(pub_b64, PEER_ID, ADDRS)
    signature = sign_reviewer_opt_in(record, priv)
    resp = await client_register_reviewer(client, server_info, record, signature)
    assert resp["type"] == "ok"
    return priv, pub_b64


def test_solicit_reviews_posts_assignment_when_pool_is_nonempty(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    uri = "test-blog-a"

    async def main():
        async with solicit_env(tmp_path) as (peer, client, server_info):
            _, rev_pub = await _register_reviewer(client, server_info)

            await peer._solicit_reviews(
                uri=uri,
                publisher_pub_b64=publisher_pub,
                publisher_private_key=publisher_priv,
                review_count=3,
                review_deadline_days=3,
                freshness_seconds=None,
            )

            listing = await client_list_assignments(client, server_info, rev_pub)
            assert len(listing["records"]) == 1
            record = listing["records"][0]["record"]
            assert record["uri"] == uri
            assert record["publisher_public_key"] == publisher_pub
            assert rev_pub in record["reviewer_public_keys"]

    _run(main)


def test_solicit_reviews_skips_gracefully_when_pool_empty(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    uri = "test-blog-b"

    async def main():
        async with solicit_env(tmp_path) as (peer, client, server_info):
            # No reviewer registered. The call must return cleanly with
            # no exceptions raised — publication should not fail.
            await peer._solicit_reviews(
                uri=uri,
                publisher_pub_b64=publisher_pub,
                publisher_private_key=publisher_priv,
                review_count=3,
                review_deadline_days=3,
                freshness_seconds=None,
            )

    _run(main)


def test_solicit_reviews_honours_freshness_filter(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    uri = "test-blog-c"

    async def main():
        async with solicit_env(tmp_path) as (peer, client, server_info):
            _, rev_pub = await _register_reviewer(client, server_info)

            # Wait so the registered entry is older than the freshness window.
            await trio.sleep(2)
            await peer._solicit_reviews(
                uri=uri,
                publisher_pub_b64=publisher_pub,
                publisher_private_key=publisher_priv,
                review_count=3,
                review_deadline_days=3,
                freshness_seconds=1,
            )

            listing = await client_list_assignments(client, server_info, rev_pub)
            assert listing["records"] == []

    _run(main)


def test_solicit_reviews_respects_review_count_cap(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    uri = "test-blog-d"

    async def main():
        async with solicit_env(tmp_path) as (peer, client, server_info):
            reviewer_pubs = []
            for _ in range(5):
                _, pub = await _register_reviewer(client, server_info)
                reviewer_pubs.append(pub)

            await peer._solicit_reviews(
                uri=uri,
                publisher_pub_b64=publisher_pub,
                publisher_private_key=publisher_priv,
                review_count=2,
                review_deadline_days=3,
                freshness_seconds=None,
            )

            # Collect all the inboxes; exactly 2 reviewers should have the
            # assignment, and the selection must be deterministic.
            selected_count = 0
            for pub in reviewer_pubs:
                listing = await client_list_assignments(client, server_info, pub)
                if listing["records"]:
                    selected_count += 1
            assert selected_count == 2

    _run(main)
