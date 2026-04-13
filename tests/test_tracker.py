import asyncio
import time

import pytest

from bundle import generate_keypair, load_private_key, public_key_to_b64, create_register_proof
from protocol import send_msg, recv_msg
from tracker import Tracker, PEER_TTL_SECONDS


def _find_free_port() -> int:
    """Find an available TCP port."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _make_register_msg(uri: str, author: str, priv_path: str) -> dict:
    """Create a valid registration message with real crypto."""
    private_key = load_private_key(priv_path)
    pub_b64 = public_key_to_b64(private_key.public_key())
    proof, ts = create_register_proof(uri, author, private_key)
    return {
        "type": "register",
        "uri": uri,
        "author": author,
        "public_key": pub_b64,
        "proof": proof,
        "timestamp": ts,
    }


async def _send_and_recv(port: int, msg: dict) -> dict:
    """Helper: open connection, send message, receive response, close."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        await send_msg(writer, msg)
        return await recv_msg(reader)
    finally:
        writer.close()
        await writer.wait_closed()


@pytest.fixture
def tracker_port():
    return _find_free_port()


@pytest.fixture
async def running_tracker(tracker_port):
    tracker = Tracker(host="127.0.0.1", port=tracker_port, redis_enabled=False)
    await tracker.start()
    yield tracker
    await tracker.close()


@pytest.mark.asyncio
class TestTracker:
    async def test_register_and_resolve(self, running_tracker, tracker_port, tmp_path):
        priv, _ = generate_keypair(str(tmp_path / "keys"), "test")
        msg = _make_register_msg("blog", "alice", priv)

        resp = await _send_and_recv(tracker_port, msg)
        assert resp["type"] == "ok"

        resp = await _send_and_recv(tracker_port, {"type": "resolve", "uri": "blog"})
        assert resp["type"] == "peers"
        assert resp["author"] == "alice"

    async def test_register_duplicate_same_key(self, running_tracker, tracker_port, tmp_path):
        priv, _ = generate_keypair(str(tmp_path / "keys"), "test")

        resp = await _send_and_recv(tracker_port, _make_register_msg("blog", "alice", priv))
        assert resp["type"] == "ok"

        resp = await _send_and_recv(tracker_port, _make_register_msg("blog", "alice", priv))
        assert resp["type"] == "ok"

    async def test_register_duplicate_different_key(self, running_tracker, tracker_port, tmp_path):
        priv1, _ = generate_keypair(str(tmp_path / "keys1"), "key1")
        priv2, _ = generate_keypair(str(tmp_path / "keys2"), "key2")

        resp = await _send_and_recv(tracker_port, _make_register_msg("blog", "alice", priv1))
        assert resp["type"] == "ok"

        resp = await _send_and_recv(tracker_port, _make_register_msg("blog", "bob", priv2))
        assert resp["type"] == "error"
        assert "different key" in resp["msg"]

    async def test_register_invalid_uri(self, running_tracker, tracker_port, tmp_path):
        priv, _ = generate_keypair(str(tmp_path / "keys"), "test")
        msg = _make_register_msg("demo", "alice", priv)
        msg["uri"] = "../../etc"

        resp = await _send_and_recv(tracker_port, msg)
        assert resp["type"] == "error"

    async def test_announce_and_list(self, running_tracker, tracker_port, tmp_path):
        priv, _ = generate_keypair(str(tmp_path / "keys"), "test")
        await _send_and_recv(tracker_port, _make_register_msg("blog", "alice", priv))

        resp = await _send_and_recv(
            tracker_port,
            {"type": "announce", "uri": "blog", "host": "127.0.0.1", "port": 5000},
        )
        assert resp["type"] == "ok"
        assert resp["peers"] == 1

        resp = await _send_and_recv(tracker_port, {"type": "list"})
        assert resp["type"] == "site_list"
        assert len(resp["sites"]) == 1

    async def test_unannounce(self, running_tracker, tracker_port, tmp_path):
        priv, _ = generate_keypair(str(tmp_path / "keys"), "test")
        await _send_and_recv(tracker_port, _make_register_msg("blog", "alice", priv))
        await _send_and_recv(
            tracker_port,
            {"type": "announce", "uri": "blog", "host": "127.0.0.1", "port": 5000},
        )

        resp = await _send_and_recv(
            tracker_port,
            {"type": "unannounce", "uri": "blog", "host": "127.0.0.1", "port": 5000},
        )
        assert resp["type"] == "ok"

        resp = await _send_and_recv(tracker_port, {"type": "resolve", "uri": "blog"})
        assert resp["type"] == "peers"
        assert len(resp["peers"]) == 0

    async def test_resolve_unknown_uri(self, running_tracker, tracker_port):
        resp = await _send_and_recv(tracker_port, {"type": "resolve", "uri": "nonexistent"})
        assert resp["type"] == "error"

    async def test_resolve_invalid_uri(self, running_tracker, tracker_port):
        resp = await _send_and_recv(tracker_port, {"type": "resolve", "uri": "../.."})
        assert resp["type"] == "error"

    async def test_unknown_message_type(self, running_tracker, tracker_port):
        resp = await _send_and_recv(tracker_port, {"type": "unknown_type"})
        assert resp["type"] == "error"
        assert "Unknown" in resp["msg"]

    async def test_rate_limiting(self, tmp_path):
        port = _find_free_port()
        tracker = Tracker(
            host="127.0.0.1",
            port=port,
            redis_enabled=False,
            rate_limit=3,
            rate_window=60.0,
        )
        await tracker.start()
        try:
            for _ in range(3):
                resp = await _send_and_recv(port, {"type": "list"})
                assert resp["type"] == "site_list"

            resp = await _send_and_recv(port, {"type": "list"})
            assert resp["type"] == "error"
            assert "Rate limit" in resp["msg"]
        finally:
            await tracker.close()

    async def test_fed_gossip(self, running_tracker, tracker_port, tmp_path):
        priv, _ = generate_keypair(str(tmp_path / "keys"), "test")
        msg = _make_register_msg("blog", "alice", priv)
        msg["type"] = "fed_gossip"
        msg["origin_tracker"] = "remote:1707"

        resp = await _send_and_recv(tracker_port, msg)
        assert resp["type"] == "ok"

        resp = await _send_and_recv(tracker_port, {"type": "resolve", "uri": "blog"})
        assert resp["type"] == "peers"
        assert resp["author"] == "alice"


@pytest.mark.asyncio
class TestTrackerPeerTTL:
    async def test_stale_peers_cleaned_up(self, tmp_path):
        port = _find_free_port()
        tracker = Tracker(host="127.0.0.1", port=port, redis_enabled=False)
        await tracker.start()
        try:
            priv, _ = generate_keypair(str(tmp_path / "keys"), "test")
            await _send_and_recv(port, _make_register_msg("blog", "alice", priv))
            await _send_and_recv(
                port,
                {"type": "announce", "uri": "blog", "host": "127.0.0.1", "port": 5000},
            )

            resp = await _send_and_recv(port, {"type": "resolve", "uri": "blog"})
            assert len(resp["peers"]) == 1

            # Backdate last-seen to simulate TTL expiry
            for key in tracker._peer_last_seen:
                tracker._peer_last_seen[key] = time.monotonic() - PEER_TTL_SECONDS - 1

            await tracker._cleanup_stale_peers()

            resp = await _send_and_recv(port, {"type": "resolve", "uri": "blog"})
            assert len(resp["peers"]) == 0
        finally:
            await tracker.close()

    async def test_reannounce_refreshes_ttl(self, tmp_path):
        port = _find_free_port()
        tracker = Tracker(host="127.0.0.1", port=port, redis_enabled=False)
        await tracker.start()
        try:
            priv, _ = generate_keypair(str(tmp_path / "keys"), "test")
            await _send_and_recv(port, _make_register_msg("blog", "alice", priv))
            await _send_and_recv(
                port,
                {"type": "announce", "uri": "blog", "host": "127.0.0.1", "port": 5000},
            )

            # Backdate, then re-announce to refresh
            for key in tracker._peer_last_seen:
                tracker._peer_last_seen[key] = time.monotonic() - PEER_TTL_SECONDS - 1

            await _send_and_recv(
                port,
                {"type": "announce", "uri": "blog", "host": "127.0.0.1", "port": 5000},
            )

            await tracker._cleanup_stale_peers()

            resp = await _send_and_recv(port, {"type": "resolve", "uri": "blog"})
            assert len(resp["peers"]) == 1
        finally:
            await tracker.close()


@pytest.mark.asyncio
class TestTrackerFederation:
    async def test_fed_gossip_stale_ignored(self, tmp_path):
        port = _find_free_port()
        tracker = Tracker(host="127.0.0.1", port=port, redis_enabled=False)
        await tracker.start()
        try:
            priv, _ = generate_keypair(str(tmp_path / "keys"), "test")
            msg = _make_register_msg("blog", "alice", priv)

            await _send_and_recv(port, msg)

            gossip = dict(msg, type="fed_gossip", origin_tracker="remote:1707")
            resp = await _send_and_recv(port, gossip)
            assert resp["type"] == "ok"
            assert "newer" in resp.get("msg", "").lower()
        finally:
            await tracker.close()
