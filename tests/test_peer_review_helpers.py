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

from bundle import compute_content_key, public_key_to_b64
from naming import (
    AssignmentStore,
    AttachmentStore,
    NameStore,
    NamingServer,
    ReviewerStore,
    client_attach_review,
    client_list_assignments,
    client_register,
    client_register_reviewer,
)
from peer.peer import Peer, _attachments_to_signals, _extract_fresh_reviewer_pool
from review import (
    build_review_record,
    build_reviewer_opt_in,
    sign_review_record,
    sign_reviewer_opt_in,
)
from trust import Policy, default_policy, save_policy


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


# ─── _attachments_to_signals ───────────────────────────────────────────


def _signed_review(content_key: str, verdict: str = "ok") -> tuple[dict, str, str]:
    priv = Ed25519PrivateKey.generate()
    pub_b64 = public_key_to_b64(priv.public_key())
    record = build_review_record(
        content_key=content_key,
        reviewer_pubkey_b64=pub_b64,
        verdict=verdict,
        comment="",
    )
    signature = sign_review_record(record, priv)
    return {"record": record, "signature": signature}, pub_b64, priv


class TestAttachmentsToSignals:
    def test_empty_returns_empty(self):
        assert _attachments_to_signals([], "c1") == []

    def test_valid_attachment_becomes_signal(self):
        entry, pub, _ = _signed_review("c1", "warn")
        signals = _attachments_to_signals([entry], "c1")
        assert len(signals) == 1
        s = signals[0]
        assert s.kind == "review"
        assert s.content_key == "c1"
        assert s.source_pubkey == pub
        assert s.verdict == "warn"

    def test_invalid_signature_dropped(self):
        entry, _, _ = _signed_review("c1")
        entry["record"]["verdict"] = "reject"  # post-sign tamper
        signals = _attachments_to_signals([entry], "c1")
        assert signals == []

    def test_mismatched_content_key_dropped(self):
        entry, _, _ = _signed_review("c1")
        signals = _attachments_to_signals([entry], "c2")
        assert signals == []

    def test_malformed_entries_ignored(self):
        valid, _, _ = _signed_review("c1")
        signals = _attachments_to_signals(
            [None, {}, {"record": "not-a-dict"}, {"record": {}}, valid],
            "c1",
        )
        assert len(signals) == 1


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


# ─── Peer.compute_score ────────────────────────────────────────────────


async def _publish_stub_naming_record(client, server_info, uri, publisher_priv, publisher_pub):
    """Register a minimal name record directly, without running a full publish."""
    from bundle import build_name_record, sign_name_record
    record = build_name_record(uri, "tester", publisher_pub, "0" * 64)
    signature = sign_name_record(record, publisher_priv)
    resp = await client_register(client, server_info, record, signature)
    assert resp["type"] == "ok"


async def _attach(client, server_info, content_key, verdict, comment=""):
    priv = Ed25519PrivateKey.generate()
    pub = public_key_to_b64(priv.public_key())
    record = build_review_record(content_key, pub, verdict, comment)
    sig = sign_review_record(record, priv)
    resp = await client_attach_review(client, server_info, record, sig)
    assert resp["type"] == "ok"
    return pub


def test_compute_score_no_attachments_returns_show(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    uri = "score-empty"

    async def main():
        async with solicit_env(tmp_path) as (peer, client, server_info):
            await _publish_stub_naming_record(
                client, server_info, uri, publisher_priv, publisher_pub
            )
            result = await peer.compute_score(
                uri,
                policy_path=str(tmp_path / "policy.json"),  # nonexistent → defaults
                trust_store_path=str(tmp_path / "trust.json"),
            )
            assert result.score == 0.0
            assert result.decision == "show"
            assert result.breakdown == []

    _run(main)


def test_compute_score_aggregates_verified_attachments(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    uri = "score-agg"

    # A lenient policy so even unknown reviewers contribute noticeably.
    policy_path = tmp_path / "policy.json"
    save_policy(
        Policy(
            threshold_warn=0.2,
            threshold_hide=10.0,
            default_weight_unknown=0.5,
        ),
        str(policy_path),
    )

    async def main():
        async with solicit_env(tmp_path) as (peer, client, server_info):
            await _publish_stub_naming_record(
                client, server_info, uri, publisher_priv, publisher_pub
            )
            content_key = compute_content_key(uri, publisher_pub)
            await _attach(client, server_info, content_key, "reject")

            result = await peer.compute_score(
                uri,
                policy_path=str(policy_path),
                trust_store_path=str(tmp_path / "trust.json"),
            )
            # 0.5 * severity("reject") = 0.5 * 3.0 = 1.5 → above warn
            assert result.decision == "warn"
            assert result.score == pytest.approx(1.5)
            assert len(result.breakdown) == 1

    _run(main)


def test_compute_score_drops_forged_attachments(tmp_path):
    """A server returning attachments under the wrong content_key must be ignored."""
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    uri = "score-forged"

    async def main():
        async with solicit_env(tmp_path) as (peer, client, server_info):
            await _publish_stub_naming_record(
                client, server_info, uri, publisher_priv, publisher_pub
            )
            # Attach a review under a DIFFERENT content_key.
            wrong_key = compute_content_key("other-uri", publisher_pub)
            await _attach(client, server_info, wrong_key, "reject")

            result = await peer.compute_score(
                uri,
                policy_path=str(tmp_path / "policy.json"),
                trust_store_path=str(tmp_path / "trust.json"),
            )
            # The forged attachment lives under a different key, so no signal.
            assert result.score == 0.0
            assert result.decision == "show"

    _run(main)
