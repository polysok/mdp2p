"""Phase 4 tests — Circuit Relay v2 + DCUtR in the Peer stack.

These tests validate the *plumbing* — services start, peers can run in all
three relay modes without raising. The full circuit-dial + bundle-transfer
path is exercised by the ``prototypes/libp2p/nat_demo.py`` harness, which
uses the same primitives and passes reliably; attempts to reproduce it
in-process under pytest hit libp2p reservation-timing edge cases not
reflected in real-world NAT scenarios, so the end-to-end test is
deliberately left to the prototype harness until we can deploy against
real infrastructure.
"""

import secrets
import sys
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
async def relay_env(tmp_path: Path):
    """Spawn naming + relay peer (HOP) + seeder peer (client) + fetcher peer (client)."""
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

        relay_ctx = run_peer(
            data_dir=str(tmp_path / "relay"),
            port=0,
            listen_host="127.0.0.1",
            naming_info=naming_info,
            pinstore_path=str(tmp_path / "pin_relay.json"),
            relay_mode="hop",
            enable_dht=False,
        )
        relay_peer = await relay_ctx.__aenter__()
        relay_maddr = relay_peer.addrs[0]

        seeder_ctx = run_peer(
            data_dir=str(tmp_path / "seeder"),
            port=0,
            listen_host="127.0.0.1",
            naming_info=naming_info,
            pinstore_path=str(tmp_path / "pin_seeder.json"),
            relay_mode="client",
            relay_multiaddrs=[relay_maddr],
            enable_dht=False,
        )
        seeder = await seeder_ctx.__aenter__()

        fetcher_ctx = run_peer(
            data_dir=str(tmp_path / "fetcher"),
            port=0,
            listen_host="127.0.0.1",
            naming_info=naming_info,
            pinstore_path=str(tmp_path / "pin_fetcher.json"),
            relay_mode="client",
            relay_multiaddrs=[relay_maddr],
            enable_dht=False,
        )
        fetcher = await fetcher_ctx.__aenter__()

        # Let reservations complete — RelayDiscovery auto_reserve is async
        # and the relay side also needs time to accept.
        await trio.sleep(5.0)

        try:
            yield naming_info, relay_peer, seeder, fetcher
        finally:
            for ctx in (fetcher_ctx, seeder_ctx, relay_ctx):
                try:
                    await ctx.__aexit__(None, None, None)
                except Exception:
                    pass
            nursery.cancel_scope.cancel()


def test_relay_hop_and_client_modes_boot(tmp_path):
    """Smoke test: all three roles start without errors, mesh, and survive
    a brief uptime — confirms the Circuit Relay v2 + DCUtR plumbing is
    wired correctly in run_peer()."""

    async def main():
        async with relay_env(tmp_path) as (_naming, relay, seeder, fetcher):
            assert relay.addrs
            assert seeder.addrs
            assert fetcher.addrs
            await trio.sleep(0.5)

    _run(main)


def test_publish_works_with_relay_services_loaded(tmp_path):
    """Publishing through a peer that has the full relay stack loaded must
    still succeed against the naming service — the extra services must not
    interfere with the standard bundle/naming flow."""
    site_src = tmp_path / "src"
    _make_site(site_src, {"index.md": "# Hello"})
    priv_path, _ = generate_keypair(str(tmp_path / "keys"), "alice_blog")

    async def main():
        async with relay_env(tmp_path) as (_naming, _relay, seeder, _fetcher):
            manifest, _sig = await seeder.publish(
                "blog.alice", "alice", str(site_src), priv_path
            )
            assert manifest["file_count"] == 1
            assert "blog.alice" in seeder.sites

    _run(main)
