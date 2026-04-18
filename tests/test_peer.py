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
