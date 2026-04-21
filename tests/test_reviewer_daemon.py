"""Tests for peer.reviewer_daemon: identity, registration, cache, poll."""

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

from bundle import compute_content_key, public_key_to_b64
from naming import (
    AssignmentStore,
    AttachmentStore,
    NameStore,
    NamingServer,
    ReviewerStore,
    client_get_attachments,
    client_list_reviewers,
    client_post_assignment,
)
from peer.reviewer_daemon import (
    AssignmentContext,
    ReviewVerdict,
    _load_cache,
    _poll_once,
    _save_cache,
    auto_decline,
    ensure_reviewer_identity,
    register_reviewer_once,
)
from review import build_review_assignment, sign_review_assignment


# ─── ensure_reviewer_identity ──────────────────────────────────────────


class TestEnsureReviewerIdentity:
    def test_creates_fresh_identity_when_missing(self, tmp_path):
        priv, pub_b64 = ensure_reviewer_identity(str(tmp_path))
        assert isinstance(priv, Ed25519PrivateKey)
        assert pub_b64 == public_key_to_b64(priv.public_key())
        assert (tmp_path / "reviewer.key").exists()
        assert (tmp_path / "reviewer.pub").exists()

    def test_reuses_existing_identity_on_second_call(self, tmp_path):
        priv1, pub1 = ensure_reviewer_identity(str(tmp_path))
        priv2, pub2 = ensure_reviewer_identity(str(tmp_path))
        assert pub1 == pub2
        # Both private keys produce the same public key.
        assert public_key_to_b64(priv1.public_key()) == public_key_to_b64(
            priv2.public_key()
        )

    def test_custom_name_isolates_identities(self, tmp_path):
        _, pub_a = ensure_reviewer_identity(str(tmp_path), name="alice")
        _, pub_b = ensure_reviewer_identity(str(tmp_path), name="bob")
        assert pub_a != pub_b


# ─── Cache helpers ─────────────────────────────────────────────────────


class TestCacheHelpers:
    def test_load_missing_returns_empty_processed(self, tmp_path):
        path = str(tmp_path / "cache.json")
        assert _load_cache(path) == {"processed": []}

    def test_save_and_load_roundtrip(self, tmp_path):
        path = str(tmp_path / "cache.json")
        _save_cache({"processed": ["k1", "k2"]}, path)
        assert _load_cache(path) == {"processed": ["k1", "k2"]}

    def test_save_creates_parent_dirs(self, tmp_path):
        path = str(tmp_path / "a" / "b" / "cache.json")
        _save_cache({"processed": []}, path)
        assert Path(path).exists()

    def test_corrupt_cache_falls_back_to_empty(self, tmp_path):
        path = tmp_path / "cache.json"
        path.write_text("not-json{", encoding="utf-8")
        assert _load_cache(str(path)) == {"processed": []}


# ─── Integration harness ───────────────────────────────────────────────


def _fresh_host():
    return new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))


@asynccontextmanager
async def daemon_env(tmp_path: Path):
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

        try:
            yield client_host, server_info
        finally:
            nursery.cancel_scope.cancel()


def _run(coro_factory):
    trio.run(coro_factory)


FAKE_PEER_ID = "12D3KooWReviewerDaemonTestPeerId111111111"
FAKE_ADDRS = ["/ip4/127.0.0.1/tcp/9001"]


# ─── register_reviewer_once ────────────────────────────────────────────


def test_register_reviewer_once_happy_path(tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = public_key_to_b64(priv.public_key())

    async def main():
        async with daemon_env(tmp_path) as (client, server_info):
            ok = await register_reviewer_once(
                client, server_info, priv, pub_b64, FAKE_PEER_ID, FAKE_ADDRS
            )
            assert ok is True

            listing = await client_list_reviewers(client, server_info)
            assert len(listing["records"]) == 1
            assert listing["records"][0]["record"]["public_key"] == pub_b64
            assert listing["records"][0]["record"]["addrs"] == FAKE_ADDRS

    _run(main)


def test_register_reviewer_updates_addrs_on_repost(tmp_path):
    priv = Ed25519PrivateKey.generate()
    pub_b64 = public_key_to_b64(priv.public_key())

    async def main():
        async with daemon_env(tmp_path) as (client, server_info):
            now = int(time.time())
            await register_reviewer_once(
                client, server_info, priv, pub_b64, FAKE_PEER_ID,
                ["/ip4/127.0.0.1/tcp/1111"], timestamp=now,
            )
            await register_reviewer_once(
                client, server_info, priv, pub_b64, FAKE_PEER_ID,
                ["/ip4/127.0.0.1/tcp/2222"], timestamp=now + 5,
            )
            listing = await client_list_reviewers(client, server_info)
            assert len(listing["records"]) == 1
            assert listing["records"][0]["record"]["addrs"] == [
                "/ip4/127.0.0.1/tcp/2222"
            ]

    _run(main)


# ─── _poll_once ────────────────────────────────────────────────────────


def _make_assignment(publisher_priv, publisher_pub, reviewer_pub, uri="test-uri"):
    deadline = int(time.time()) + 3 * 86400
    record = build_review_assignment(
        uri=uri,
        publisher_pubkey_b64=publisher_pub,
        reviewer_pubkeys_b64=[reviewer_pub],
        deadline=deadline,
    )
    signature = sign_review_assignment(record, publisher_priv)
    return record, signature


def test_poll_once_no_assignments_is_noop(tmp_path):
    reviewer_priv = Ed25519PrivateKey.generate()
    reviewer_pub = public_key_to_b64(reviewer_priv.public_key())
    cache_path = str(tmp_path / "cache.json")

    async def fetch(_a):
        raise AssertionError("fetch must not be called when inbox is empty")

    async def cb(_ctx):
        raise AssertionError("callback must not be called when inbox is empty")

    async def main():
        async with daemon_env(tmp_path) as (client, server_info):
            await _poll_once(
                client, server_info, reviewer_priv, reviewer_pub, fetch, cb, cache_path
            )
            assert _load_cache(cache_path) == {"processed": []}

    _run(main)


def test_poll_once_processes_assignment_and_attaches_verdict(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    reviewer_priv = Ed25519PrivateKey.generate()
    reviewer_pub = public_key_to_b64(reviewer_priv.public_key())
    cache_path = str(tmp_path / "cache.json")

    record, signature = _make_assignment(publisher_priv, publisher_pub, reviewer_pub)
    content_key = record["content_key"]
    fake_manifest = {"uri": "test-uri", "files": [], "public_key": publisher_pub}

    callback_seen = []

    async def fetch(assignment):
        return fake_manifest

    async def cb(ctx: AssignmentContext):
        callback_seen.append(ctx)
        return ReviewVerdict(verdict="warn", comment="looks good")

    async def main():
        async with daemon_env(tmp_path) as (client, server_info):
            await client_post_assignment(client, server_info, record, signature)

            await _poll_once(
                client, server_info, reviewer_priv, reviewer_pub, fetch, cb, cache_path
            )

            # Callback saw the assignment + manifest.
            assert len(callback_seen) == 1
            assert callback_seen[0].assignment == record
            assert callback_seen[0].manifest == fake_manifest

            # Review is attached on the server side.
            listing = await client_get_attachments(client, server_info, content_key)
            assert len(listing["records"]) == 1
            attached = listing["records"][0]["record"]
            assert attached["verdict"] == "warn"
            assert attached["comment"] == "looks good"
            assert attached["reviewer_public_key"] == reviewer_pub

            # Cache remembers it was processed.
            assert content_key in _load_cache(cache_path)["processed"]

    _run(main)


def test_poll_once_callback_decline_marks_processed_without_attach(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    reviewer_priv = Ed25519PrivateKey.generate()
    reviewer_pub = public_key_to_b64(reviewer_priv.public_key())
    cache_path = str(tmp_path / "cache.json")

    record, signature = _make_assignment(publisher_priv, publisher_pub, reviewer_pub)
    content_key = record["content_key"]

    async def fetch(_a):
        return {"uri": "test-uri", "files": [], "public_key": publisher_pub}

    async def main():
        async with daemon_env(tmp_path) as (client, server_info):
            await client_post_assignment(client, server_info, record, signature)

            await _poll_once(
                client, server_info, reviewer_priv, reviewer_pub, fetch,
                auto_decline, cache_path,
            )

            listing = await client_get_attachments(client, server_info, content_key)
            assert listing["records"] == []  # no attachment

            assert content_key in _load_cache(cache_path)["processed"]

    _run(main)


def test_poll_once_skips_already_processed(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    reviewer_priv = Ed25519PrivateKey.generate()
    reviewer_pub = public_key_to_b64(reviewer_priv.public_key())
    cache_path = str(tmp_path / "cache.json")

    record, signature = _make_assignment(publisher_priv, publisher_pub, reviewer_pub)
    content_key = record["content_key"]
    _save_cache({"processed": [content_key]}, cache_path)

    fetch_called = [False]

    async def fetch(_a):
        fetch_called[0] = True
        return {}

    async def cb(_ctx):
        raise AssertionError("callback must not run for already-processed keys")

    async def main():
        async with daemon_env(tmp_path) as (client, server_info):
            await client_post_assignment(client, server_info, record, signature)

            await _poll_once(
                client, server_info, reviewer_priv, reviewer_pub, fetch, cb, cache_path
            )

            assert fetch_called[0] is False

    _run(main)


def test_poll_once_skips_expired_assignment(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    reviewer_priv = Ed25519PrivateKey.generate()
    reviewer_pub = public_key_to_b64(reviewer_priv.public_key())
    cache_path = str(tmp_path / "cache.json")

    # Build an assignment with a deadline already in the past.
    now = int(time.time())
    record = build_review_assignment(
        uri="expired",
        publisher_pubkey_b64=publisher_pub,
        reviewer_pubkeys_b64=[reviewer_pub],
        deadline=now + 60,  # valid at post time
        timestamp=now,
    )
    signature = sign_review_assignment(record, publisher_priv)
    content_key = record["content_key"]

    async def fetch(_a):
        raise AssertionError("expired assignment should not trigger fetch")

    async def cb(_ctx):
        raise AssertionError("expired assignment should not trigger callback")

    async def main():
        async with daemon_env(tmp_path) as (client, server_info):
            await client_post_assignment(client, server_info, record, signature)

            # Poll 70 seconds "later" by fabricating the cache manually after
            # a manual deadline shift. Simpler: we poll immediately, then
            # re-poll after manipulating the stored record via another post
            # — or we just use a tiny deadline and sleep. We'll sleep to
            # keep the test honest but bounded.
            await trio.sleep(0)  # no sleep needed: deadline check uses now()
            # To actually hit the expired branch, post a fresh assignment
            # with deadline = now - 1:
            expired_record = dict(record)
            # Rebuild with past deadline — we need a valid signature, so
            # we build a new one with deadline just past the drift window.
            past = build_review_assignment(
                uri="expired-2",
                publisher_pubkey_b64=publisher_pub,
                reviewer_pubkeys_b64=[reviewer_pub],
                deadline=1,  # unambiguously in the past
                timestamp=now,
            )
            past_sig = sign_review_assignment(past, publisher_priv)
            await client_post_assignment(client, server_info, past, past_sig)

            await _poll_once(
                client, server_info, reviewer_priv, reviewer_pub, fetch, cb, cache_path
            )

            # The expired key is marked processed so we never look at it again.
            processed = _load_cache(cache_path)["processed"]
            assert past["content_key"] in processed

    _run(main)


def test_poll_once_retries_when_content_unavailable(tmp_path):
    publisher_priv = Ed25519PrivateKey.generate()
    publisher_pub = public_key_to_b64(publisher_priv.public_key())
    reviewer_priv = Ed25519PrivateKey.generate()
    reviewer_pub = public_key_to_b64(reviewer_priv.public_key())
    cache_path = str(tmp_path / "cache.json")

    record, signature = _make_assignment(publisher_priv, publisher_pub, reviewer_pub)
    content_key = record["content_key"]

    async def fetch(_a):
        return None  # content not yet available

    async def cb(_ctx):
        raise AssertionError("callback must not run when content is unavailable")

    async def main():
        async with daemon_env(tmp_path) as (client, server_info):
            await client_post_assignment(client, server_info, record, signature)

            await _poll_once(
                client, server_info, reviewer_priv, reviewer_pub, fetch, cb, cache_path
            )

            # Not marked processed — will be retried next poll cycle.
            assert content_key not in _load_cache(cache_path)["processed"]

    _run(main)
