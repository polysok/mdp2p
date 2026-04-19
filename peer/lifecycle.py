"""Process-level lifecycle for a running Peer.

``run_peer`` is the single entry point every caller uses to stand up a
host, DHT, optional Circuit Relay v2 stack, and a Peer bound to them —
all cleanly torn down on context exit. ``link_peers_dht`` is a tiny
test helper that full-meshes the routing tables of several peers.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import AsyncIterator, Optional

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.kad_dht.kad_dht import DHTMode, KadDHT
from libp2p.peer.peerinfo import PeerInfo
from libp2p.tools.async_service import background_trio_service

from naming import load_or_create_peer_seed

from .host_factory import (
    bootstrap_dht,
    build_circuit_stack,
    connect_relays,
    detect_local_ip,
)
from .peer import DEFAULT_DATA_DIR, DEFAULT_PINSTORE, Peer

logger = logging.getLogger("mdp2p.peer.lifecycle")


@asynccontextmanager
async def run_peer(
    data_dir: str = DEFAULT_DATA_DIR,
    port: int = 0,
    listen_host: Optional[str] = None,
    peer_key_path: Optional[str] = None,
    naming_info: Optional[PeerInfo] = None,
    pinstore_path: str = DEFAULT_PINSTORE,
    bootstrap_multiaddrs: Optional[list[str]] = None,
    enable_dht: bool = True,
    relay_mode: str = "none",
    relay_multiaddrs: Optional[list[str]] = None,
) -> AsyncIterator[Peer]:
    """Run a Peer with a libp2p host, DHT, and optional Circuit Relay v2 stack.

    - `bootstrap_multiaddrs` : peers dialed at startup; they seed the DHT
      routing table so provide/find_providers have propagation targets.
    - `enable_dht=False` disables the DHT entirely (useful for tests that
      wire peers manually without discovery).
    - `relay_mode` : one of
        - "none" (default): no Circuit Relay services loaded
        - "client": STOP + CLIENT roles. The peer can dial and accept
          connections through a relay, and attempts DCUtR upgrades.
        - "hop": full relay (HOP + STOP + CLIENT). For the peer-zero VPS.
    - `relay_multiaddrs` : list of relay multiaddrs to dial at startup
      (client mode only); RelayDiscovery will auto-reserve slots.
    - `listen_host` : bind address. Defaults to the auto-detected LAN IP
      (see detect_local_ip) to avoid polluting the DHT with ``0.0.0.0``
      peer records. Public hosts (the peer-zero VPS) should explicitly
      pass ``"0.0.0.0"`` to accept connections from anywhere.
    """
    if relay_mode not in ("none", "client", "hop"):
        raise ValueError(
            f"relay_mode must be 'none', 'client' or 'hop', got {relay_mode!r}"
        )

    if listen_host is None:
        listen_host = detect_local_ip()

    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    key_path = peer_key_path or str(data_path / "peer.key")

    seed = load_or_create_peer_seed(key_path)
    host = new_host(key_pair=create_new_key_pair(seed))
    listen = [multiaddr.Multiaddr(f"/ip4/{listen_host}/tcp/{port}")]

    dht = KadDHT(host, DHTMode.SERVER) if enable_dht else None

    async with host.run(listen_addrs=listen), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)

        async with AsyncExitStack() as stack:
            if dht is not None:
                await stack.enter_async_context(background_trio_service(dht))

            relay_transport = await build_circuit_stack(host, relay_mode, stack)

            peer = Peer(
                host,
                data_dir=data_dir,
                naming_info=naming_info,
                pinstore_path=pinstore_path,
                dht=dht,
                relay_transport=relay_transport,
            )
            peer.attach()
            await bootstrap_dht(host, dht, bootstrap_multiaddrs, logger)
            await connect_relays(host, relay_multiaddrs, logger)

            try:
                yield peer
            finally:
                nursery.cancel_scope.cancel()


async def link_peers_dht(*peers: Peer) -> None:
    """Full-mesh the DHT routing tables of the given peers.

    py-libp2p 0.6.0 doesn't auto-populate the routing table from inbound
    connections, so peers that dial each other still need an explicit
    add_peer call. This helper is mostly for tests and small deployments.
    """
    for a in peers:
        for b in peers:
            if a is b or a.dht is None or b.dht is None:
                continue
            b_id = b.host.get_id()
            for addr in b.host.get_addrs():
                a.host.get_peerstore().add_addrs(b_id, [addr], 3600)
            try:
                info = PeerInfo(b_id, list(b.host.get_addrs()))
                await a.host.connect(info)
            except Exception:
                pass
            await a.dht.routing_table.add_peer(b_id)
