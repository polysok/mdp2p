"""Phase 3 tests — DHT-based swarm discovery replaces the old tracker peer list."""

import secrets
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.utils.address_validation import find_free_port

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bundle import generate_keypair
from naming import NameStore, NamingServer, load_or_create_peer_seed
from peer import link_peers_dht, run_peer


def _run(coro_factory):
    trio.run(coro_factory)


def _make_site(site_dir: Path, pages: dict[str, str]) -> None:
    site_dir.mkdir(parents=True, exist_ok=True)
    for name, content in pages.items():
        (site_dir / name).write_text(content, encoding="utf-8")


@asynccontextmanager
async def dht_env(tmp_path: Path, n_peers: int):
    """Spin up naming + n DHT-enabled peers, all meshed at the DHT level."""
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

        peer_ctxs = [
            run_peer(
                data_dir=str(tmp_path / f"peer_{i}"),
                port=0,
                naming_info=naming_info,
                pinstore_path=str(tmp_path / f"pin_{i}.json"),
            )
            for i in range(n_peers)
        ]
        peers = [await ctx.__aenter__() for ctx in peer_ctxs]

        # Full-mesh DHT routing tables so provide/find work in the 3-peer demo.
        await link_peers_dht(*peers)
        await trio.sleep(0.5)

        try:
            yield naming_info, peers
        finally:
            for ctx in reversed(peer_ctxs):
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass
            nursery.cancel_scope.cancel()


def test_fetch_uses_dht_when_seeder_addrs_omitted(tmp_path):
    site_src = tmp_path / "src"
    _make_site(site_src, {"index.md": "# Hello"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with dht_env(tmp_path, n_peers=2) as (_naming, peers):
            alice, bob = peers

            await alice.publish("blog.alice", "alice", str(site_src), priv_path)
            # propagation delay for provider record
            await trio.sleep(1.0)

            # No seeder_addrs passed — bob must find alice via DHT.
            ok = await bob.fetch_site("blog.alice")
            assert ok is True
            assert (Path(bob.sites["blog.alice"]) / "index.md").exists()

    _run(main)


def test_swarm_grows_via_dht(tmp_path):
    site_src = tmp_path / "src"
    _make_site(site_src, {"index.md": "# Hello\nWorld."})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with dht_env(tmp_path, n_peers=3) as (_naming, peers):
            alice, bob, charlie = peers

            await alice.publish("blog.alice", "alice", str(site_src), priv_path)
            await trio.sleep(1.0)

            assert await bob.fetch_site("blog.alice")
            # Bob is now a seeder — ensure his announce() had time to propagate.
            await trio.sleep(2.0)

            assert await charlie.fetch_site("blog.alice")

            # Charlie's DHT lookup should have seen BOTH alice and bob at some point.
            manifest_loc = Path(charlie.sites["blog.alice"]) / "index.md"
            assert manifest_loc.read_text() == "# Hello\nWorld."

    _run(main)


def test_explicit_seeder_addrs_bypasses_dht(tmp_path):
    """Explicit seeder_addrs must take precedence over DHT lookup."""
    site_src = tmp_path / "src"
    _make_site(site_src, {"index.md": "# Hello"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with dht_env(tmp_path, n_peers=2) as (_naming, peers):
            alice, bob = peers
            await alice.publish("blog.alice", "alice", str(site_src), priv_path)

            # Immediately fetch with explicit addrs — no sleep, no DHT propagation needed.
            ok = await bob.fetch_site("blog.alice", seeder_addrs=alice.addrs)
            assert ok is True

    _run(main)
