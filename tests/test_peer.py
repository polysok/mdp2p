"""Tests for the new trio+libp2p peer (publish / fetch / check_for_update / TOFU)."""

import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import multiaddr
import trio
from libp2p.peer.peerinfo import info_from_p2p_addr

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bundle import generate_keypair, load_bundle, load_private_key, public_key_to_b64
from naming import NameStore, NamingServer, load_or_create_peer_seed
from peer import Peer, run_peer
from pinstore import load_pinstore, pin_key


@asynccontextmanager
async def naming_and_peers(tmp_path: Path, n_peers: int = 2):
    """Spawn a naming server + n peers; yield (naming_info, [peer, ...])."""
    from libp2p import new_host
    from libp2p.crypto.ed25519 import create_new_key_pair
    from libp2p.utils.address_validation import find_free_port

    naming_port = find_free_port()
    naming_seed = load_or_create_peer_seed(str(tmp_path / "naming.key"))
    naming_host = new_host(key_pair=create_new_key_pair(naming_seed))

    async with (
        naming_host.run(
            listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{naming_port}")]
        ),
        trio.open_nursery() as nursery,
    ):
        nursery.start_soon(naming_host.get_peerstore().start_cleanup_task, 60)

        store = NameStore(str(tmp_path / "naming_records.json"))
        server = NamingServer(naming_host, store)
        server.attach()

        naming_maddr = (
            f"/ip4/127.0.0.1/tcp/{naming_port}/p2p/"
            f"{naming_host.get_id().to_string()}"
        )
        naming_info = info_from_p2p_addr(multiaddr.Multiaddr(naming_maddr))

        peers: list = []

        async def setup_peers():
            async with _stack_peers(tmp_path, n_peers, naming_info) as peer_list:
                peers.extend(peer_list)
                yield

        # We need nested async context. Use a manual approach.
        # Spawn peers in their own nursery task, then yield.
        peer_stack_done = trio.Event()
        peer_ready = trio.Event()
        peer_teardown = trio.Event()
        built_peers: list = []

        async def peer_lifecycle():
            async with _stack_peers(tmp_path, n_peers, naming_info) as peer_list:
                built_peers.extend(peer_list)
                peer_ready.set()
                await peer_teardown.wait()
            peer_stack_done.set()

        nursery.start_soon(peer_lifecycle)
        await peer_ready.wait()

        try:
            yield naming_info, built_peers
        finally:
            peer_teardown.set()
            await peer_stack_done.wait()
            nursery.cancel_scope.cancel()


@asynccontextmanager
async def _stack_peers(tmp_path: Path, n_peers: int, naming_info):
    """Start n peers sequentially and yield the list."""
    if n_peers == 0:
        yield []
        return

    peer_ctxs = []
    for i in range(n_peers):
        ctx = run_peer(
            data_dir=str(tmp_path / f"peer_{i}"),
            port=0,
            naming_info=naming_info,
            pinstore_path=str(tmp_path / f"pin_{i}.json"),
        )
        peer_ctxs.append(ctx)

    async with trio.open_nursery():
        peers = []
        # Enter each context manager individually and hold them open.
        aexits = []
        for ctx in peer_ctxs:
            peer = await ctx.__aenter__()
            peers.append(peer)
            aexits.append(ctx)
        try:
            yield peers
        finally:
            # Close in reverse order
            for ctx in reversed(aexits):
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass


def _make_site(site_dir: Path, pages: dict[str, str]) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    for name, content in pages.items():
        (site_dir / name).write_text(content, encoding="utf-8")


def _run(coro_factory):
    trio.run(coro_factory)


# ─── Happy path ─────────────────────────────────────────────────────────

def test_publish_then_fetch(tmp_path):
    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# Hello\nWorld.", "about.md": "# About"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=2) as (naming_info, peers):
            alice, bob = peers

            manifest, _ = await alice.publish(
                "blog.alice", "alice", str(site_src), priv_path
            )
            assert manifest["file_count"] == 2
            assert "blog.alice" in alice.sites

            ok = await bob.fetch_site("blog.alice", seeder_addrs=alice.addrs)
            assert ok is True
            assert "blog.alice" in bob.sites

            fetched_index = Path(bob.sites["blog.alice"]) / "index.md"
            assert fetched_index.read_text() == "# Hello\nWorld."

            # Pin should exist now
            pinstore = load_pinstore(bob.pinstore_path)
            assert "blog.alice" in pinstore

    _run(main)


def test_fetch_unknown_uri_returns_false(tmp_path):
    async def main():
        async with naming_and_peers(tmp_path, n_peers=1) as (naming_info, peers):
            (bob,) = peers
            # No one has registered "ghost.site"; naming resolve → error.
            # seeder_addrs is also empty but the naming resolve failure comes first.
            ok = await bob.fetch_site("ghost.site", seeder_addrs=["/ip4/127.0.0.1/tcp/1/p2p/anything"])
            assert ok is False

    _run(main)


# ─── Anti-MITM (TOFU) ──────────────────────────────────────────────────

def test_tofu_mismatch_rejects_fetch(tmp_path):
    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# Hello"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    # Pre-populate Bob's pinstore with a WRONG key for blog.alice.
    # Bob's pinstore_path is computed as tmp_path / "pin_0.json" by _stack_peers.
    fake_pubkey_b64 = public_key_to_b64(load_private_key(priv_path).public_key())
    # Flip one byte → guaranteed mismatch
    corrupted = bytearray(fake_pubkey_b64.encode())
    corrupted[0] = (corrupted[0] + 1) % 256
    corrupted_pub_b64 = corrupted.decode(errors="replace")

    pinstore_path = tmp_path / "pin_1.json"  # bob is peer index 1
    pin_key("blog.alice", corrupted_pub_b64, "attacker", str(pinstore_path))

    async def main():
        async with naming_and_peers(tmp_path, n_peers=2) as (naming_info, peers):
            alice, bob = peers
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)
            # Bob should refuse: the naming record's pubkey is Alice's real one,
            # but his pinstore has the corrupted one → MISMATCH.
            ok = await bob.fetch_site("blog.alice", seeder_addrs=alice.addrs)
            assert ok is False

    _run(main)


# ─── Update flow ───────────────────────────────────────────────────────

def test_check_for_update_after_new_version(tmp_path):
    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# v1"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=2) as (naming_info, peers):
            alice, bob = peers
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)
            assert await bob.fetch_site("blog.alice", seeder_addrs=alice.addrs)

            # Alice publishes v2: change a file, bump version happens inside publish()
            (site_src / "index.md").write_text("# v2 with more stuff", encoding="utf-8")
            # publish() loads manifest.json from the *source* directory to bump
            # version — need to ensure the sign -> save step wrote manifest.json
            # in site_src already. Yes, save_bundle writes into site_src.
            # Give the timestamp at least 1 second room:
            time.sleep(1.1)
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)

            has_update = await bob.check_for_update("blog.alice", alice.addrs[0])
            assert has_update is True

    _run(main)


# ─── Author tamper / replay protections ─────────────────────────────────

def test_manifest_ref_mismatch_aborts(tmp_path):
    """If the naming record points to a different manifest than what the
    seeder serves, the fetcher must abort (aka content binding)."""
    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# Hello"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=2) as (naming_info, peers):
            alice, bob = peers
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)

            # Modify the seeded file on Alice's side WITHOUT re-publishing, then
            # rewrite the manifest so the signature check passes but the ref
            # no longer matches naming.
            # Simpler: tamper Alice's stored manifest.json after publish. The
            # seeder will serve it, and the naming record still points to the
            # original ref → fetch must fail.
            site_served = Path(alice.sites["blog.alice"])
            manifest, sig = load_bundle(str(site_served))
            manifest["version"] = 999  # post-signing tamper
            (site_served / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            # Leave the signature untouched — verify_manifest will reject it too,
            # but the flow reaches the ref check first on the happy path.

            ok = await bob.fetch_site("blog.alice", seeder_addrs=alice.addrs)
            assert ok is False

    _run(main)


# ─── Hostile seeders / naming ───────────────────────────────────────────

def test_fetch_skips_tampering_seeder_and_uses_honest_one(tmp_path):
    """A single bad provider must not make the fetch fail (DHT poisoning)."""
    import shutil

    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# Genuine"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=3) as (naming_info, peers):
            alice, mallory, bob = peers
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)

            # Mallory seeds a copy with the genuine signed manifest but
            # altered content.
            evil_dir = tmp_path / "mallory_copy"
            shutil.copytree(alice.sites["blog.alice"], evil_dir)
            (evil_dir / "index.md").write_text("# Pwned", encoding="utf-8")
            mallory.sites["blog.alice"] = str(evil_dir)

            ok = await bob.fetch_site(
                "blog.alice", seeder_addrs=mallory.addrs + alice.addrs
            )
            assert ok is True
            fetched = Path(bob.sites["blog.alice"]) / "index.md"
            assert fetched.read_text() == "# Genuine"

    _run(main)


def test_fetch_rejects_record_for_another_uri(tmp_path, monkeypatch):
    """A naming server must not be able to answer 'bank' with 'evil'."""
    import peer.peer as peer_module

    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# Evil"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "mallory_evil")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=2) as (naming_info, peers):
            mallory, bob = peers
            await mallory.publish("evil", "mallory", str(site_src), priv_path)
            # Mallory also answers bundle requests for "bank" with her site.
            mallory.sites["bank"] = mallory.sites["evil"]

            real_resolve = peer_module.naming_resolve

            async def lying_resolve(host, info, uri):
                return await real_resolve(host, info, "evil")

            monkeypatch.setattr(peer_module, "naming_resolve", lying_resolve)
            ok = await bob.fetch_site("bank", seeder_addrs=mallory.addrs)
            assert ok is False
            assert "bank" not in load_pinstore(bob.pinstore_path)

    _run(main)


def test_download_does_not_hang_on_stalling_seeder(tmp_path):
    """A seeder that never answers is abandoned after the timeout."""
    from peer.bundle_protocol import BUNDLE_PROTOCOL, try_download_from_seeders

    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# Hello"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=3) as (naming_info, peers):
            alice, staller, bob = peers
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)

            async def never_answer(stream):
                await trio.sleep_forever()

            staller.host.set_stream_handler(BUNDLE_PROTOCOL, never_answer)

            import logging

            with trio.fail_after(10):
                stalled_only = await try_download_from_seeders(
                    bob.host, None, "blog.alice", staller.addrs[:1],
                    logging.getLogger("test"), timeout=1.0,
                )
                assert stalled_only is None

                result = await try_download_from_seeders(
                    bob.host, None, "blog.alice",
                    staller.addrs[:1] + alice.addrs[:1],
                    logging.getLogger("test"), timeout=5.0,
                )
                assert result is not None
                assert result["manifest"]["uri"] == "blog.alice"

    _run(main)


def test_check_for_update_ignores_unsigned_newer_manifest(tmp_path):
    """A seeder cannot fake 'update available' with an unsigned timestamp."""
    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# v1"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=2) as (naming_info, peers):
            alice, bob = peers
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)
            assert await bob.fetch_site("blog.alice", seeder_addrs=alice.addrs)

            served = Path(alice.sites["blog.alice"])
            manifest, _ = load_bundle(str(served))
            manifest["timestamp"] += 3600  # forged, signature left untouched
            (served / "manifest.json").write_text(json.dumps(manifest))

            assert await bob.check_for_update("blog.alice", alice.addrs[0]) is False

    _run(main)


def test_fetch_rejects_rolled_back_naming_record(tmp_path, monkeypatch):
    """Once v2 was seen, replaying the (still validly signed) v1 is refused.

    Mallory kept a copy of v1 and the naming server replays the v1 record:
    every signature and ref check passes, only freshness can catch it.
    """
    import shutil

    import peer.peer as peer_module

    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# v1"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=3) as (naming_info, peers):
            alice, mallory, bob = peers
            real_resolve = peer_module.naming_resolve

            await alice.publish("blog.alice", "alice", str(site_src), priv_path)
            v1_resp = await real_resolve(bob.host, naming_info, "blog.alice")
            v1_copy = tmp_path / "mallory_v1"
            shutil.copytree(site_src, v1_copy)
            mallory.sites["blog.alice"] = str(v1_copy)

            (site_src / "index.md").write_text("# v2", encoding="utf-8")
            time.sleep(1.1)
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)
            assert await bob.fetch_site("blog.alice", seeder_addrs=alice.addrs)

            async def replay_v1(host, info, uri):
                return v1_resp

            monkeypatch.setattr(peer_module, "naming_resolve", replay_v1)
            ok = await bob.fetch_site("blog.alice", seeder_addrs=mallory.addrs)
            assert ok is False
            fetched = Path(bob.sites["blog.alice"]) / "index.md"
            assert fetched.read_text() == "# v2"

    _run(main)


def test_fetch_fails_closed_on_corrupted_pinstore(tmp_path):
    site_src = tmp_path / "src_site"
    _make_site(site_src, {"index.md": "# Hello"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")
    (tmp_path / "pin_1.json").write_text("{not json", encoding="utf-8")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=2) as (naming_info, peers):
            alice, bob = peers
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)
            ok = await bob.fetch_site("blog.alice", seeder_addrs=alice.addrs)
            assert ok is False


def test_fetch_bundle_larger_than_noise_frame(tmp_path):
    """Bundles above 64 KiB used to fail: Noise caps a single write at 65535."""
    site_src = tmp_path / "src_site"
    big_page = "# Big\n" + "lorem ipsum dolor sit amet\n" * 10_000  # ~270 KB
    _make_site(site_src, {"index.md": big_page})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with naming_and_peers(tmp_path, n_peers=2) as (naming_info, peers):
            alice, bob = peers
            await alice.publish("big.alice", "alice", str(site_src), priv_path)
            assert await bob.fetch_site("big.alice", seeder_addrs=alice.addrs)
            fetched = Path(bob.sites["big.alice"]) / "index.md"
            assert fetched.read_text() == big_page

    _run(main)
