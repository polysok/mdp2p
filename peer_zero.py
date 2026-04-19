"""
MDP2P peer-zero — combined naming server + Circuit Relay v2 HOP + DHT
bootstrap on a single libp2p host.

Intended to run on a public-IP VPS (e.g. relay.mdp2p.net) so that:
  - Visitors behind NAT can reach each other through Circuit Relay v2
  - New peers can bootstrap their DHT routing table through this node
  - A single stable multiaddr hosts the naming protocol too, so clients
    only need one address to join the network.

Usage (local):
    python peer_zero.py --port 1707 --data-dir ./peer_zero_data

Usage (production VPS):
    python peer_zero.py --port 1707 \\
        --data-dir /var/lib/mdp2p/peer_zero \\
        --listen-host 0.0.0.0
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import trio

from naming import NameStore, NamingServer
from peer import run_peer

DEFAULT_PORT = 1707
DEFAULT_DATA_DIR = "./peer_zero_data"


async def serve(port: int, data_dir: str, listen_host: str) -> None:
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    naming_store_path = data_path / "naming_records.json"
    peer_key_path = data_path / "peer.key"

    async with run_peer(
        data_dir=data_dir,
        port=port,
        listen_host=listen_host,
        peer_key_path=str(peer_key_path),
        naming_info=None,
        relay_mode="hop",
    ) as peer:
        # Attach the naming server to the same host so both protocols
        # live behind a single multiaddr / peer-id.
        store = NameStore(str(naming_store_path))
        naming_server = NamingServer(peer.host, store)
        naming_server.attach()

        peer_id = peer.host.get_id().to_string()
        print("━" * 70)
        print("  MDP2P peer-zero")
        print("  Services : naming + circuit relay v2 (HOP) + DHT bootstrap")
        print(f"  Port     : {port}")
        print(f"  PeerID   : {peer_id}")
        print(f"  Store    : {naming_store_path} ({len(store.list_records())} records)")
        for addr in peer.addrs:
            print(f"  Listen   : {addr}")
        print(f"  Bootstrap: /dns4/<your-host>/tcp/{port}/p2p/{peer_id}")
        print("━" * 70)

        await trio.sleep_forever()


def main() -> None:
    parser = argparse.ArgumentParser(description="MDP2P peer-zero")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument(
        "--listen-host",
        default="0.0.0.0",
        help="Bind address (use your public IP on production, 0.0.0.0 otherwise)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [PEER0] %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence py-libp2p's internal retry noise on stale DHT peer records.
    for noisy in (
        "libp2p.transport.tcp",
        "libp2p.kad_dht.peer_routing",
        "libp2p.host.basic_host",
    ):
        logging.getLogger(noisy).setLevel(logging.CRITICAL)

    try:
        trio.run(serve, args.port, args.data_dir, args.listen_host)
    except KeyboardInterrupt:
        print("\n[PEER0] stopped")
        sys.exit(0)


if __name__ == "__main__":
    main()
