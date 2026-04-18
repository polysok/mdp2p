#!/usr/bin/env python3
"""
MDP2P Publish — register a Markdown site on the naming server and seed it.

Usage:
    python publish.py --uri blog --author alice \\
        --site ./mon_site --keys ./cles \\
        --naming /ip4/127.0.0.1/tcp/1707/p2p/<NAMING_PEER_ID>
"""

import argparse
import logging
import sys
from pathlib import Path

import multiaddr
import trio
from libp2p.peer.peerinfo import info_from_p2p_addr

sys.path.insert(0, str(Path(__file__).parent))

from bundle import generate_keypair, make_key_name
from peer import run_peer


async def main(
    uri: str,
    author: str,
    site_dir: str,
    keys_dir: str,
    naming_multiaddr: str,
    port: int,
) -> None:
    keys_path = Path(keys_dir)
    keys_path.mkdir(parents=True, exist_ok=True)

    key_name = make_key_name(author, uri)
    priv_path = keys_path / f"{key_name}.key"

    if priv_path.exists():
        print(f"[PUBLISH] Using existing key: {priv_path}")
    else:
        priv_path_str, pub_path_str = generate_keypair(keys_dir, key_name)
        print(f"[PUBLISH] Key generated: {priv_path_str}")
        print(f"[PUBLISH] Public key   : {pub_path_str}")

    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(naming_multiaddr))

    async with run_peer(
        data_dir=f"./peer_data_{key_name}",
        port=port,
        naming_info=naming_info,
    ) as peer:
        print(f"[PUBLISH] Peer ID      : {peer.host.get_id().to_string()}")
        print("[PUBLISH] Listening on :")
        for addr in peer.addrs:
            print(f"            {addr}")

        print(f"[PUBLISH] Registering {author}@{uri} via {naming_multiaddr}")
        await peer.publish(uri, author, site_dir, str(priv_path))

        print("[PUBLISH] Seeding. Share one of the above addresses to let peers fetch.")
        print("[PUBLISH] Press Ctrl+C to stop.")
        await trio.sleep_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Publish a Markdown site on the MDP2P naming service"
    )
    parser.add_argument("--uri", required=True, help="URI (e.g., blog)")
    parser.add_argument("--author", required=True, help="Author name (e.g., alice)")
    parser.add_argument("--site", required=True, help="Directory containing .md files")
    parser.add_argument(
        "--keys", default="./keys", help="Directory for author keys (default: ./keys)"
    )
    parser.add_argument(
        "--naming",
        required=True,
        help="Naming server multiaddr (e.g., /ip4/1.2.3.4/tcp/1707/p2p/12D3Koo...)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Seeder port (0 = auto-assign, default: 0)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        trio.run(
            main,
            args.uri,
            args.author,
            args.site,
            args.keys,
            args.naming,
            args.port,
        )
    except KeyboardInterrupt:
        print("\n[PUBLISH] Stopped.")
