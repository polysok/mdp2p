#!/usr/bin/env python3
"""
MDP2P Publish — Register a Markdown site on a tracker.

Usage:
    python publish.py --uri md://blog --site ./mon_site --keys ./cles --tracker localhost:1707
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from bundle import generate_keypair, load_private_key, make_key_name, public_key_to_b64
from peer import Peer


async def main(
    uri: str,
    author: str,
    site_dir: str,
    keys_dir: str,
    tracker_host: str,
    tracker_port: int,
    port: int,
):
    keys_path = Path(keys_dir)
    keys_path.mkdir(parents=True, exist_ok=True)

    key_name = make_key_name(author, uri)
    priv_path = keys_path / f"{key_name}.key"

    if priv_path.exists():
        print(f"[PUBLISH] Using existing key: {priv_path}")
    else:
        priv_path_str, pub_path_str = generate_keypair(keys_dir, key_name)
        print(f"[PUBLISH] Key generated: {priv_path_str}")
        print(f"[PUBLISH] Public key: {pub_path_str}")

    peer = Peer(
        data_dir=f"./peer_data_{key_name}",
        host="0.0.0.0",
        port=port,
        tracker_host=tracker_host,
        tracker_port=tracker_port,
    )
    await peer.start_seeding()

    print(f"[PUBLISH] Registering {author}@{uri} on {tracker_host}:{tracker_port}...")
    await peer.publish(uri, author, site_dir, str(priv_path))

    print(f"[PUBLISH] Seeding on port {peer.port}")
    print("[PUBLISH] Press Ctrl+C to stop.")
    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        pass
    finally:
        await peer.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Publish a Markdown site on MDP2P tracker"
    )
    parser.add_argument("--uri", required=True, help="URI (e.g., md://blog)")
    parser.add_argument("--author", required=True, help="Author name (e.g., alice)")
    parser.add_argument("--site", required=True, help="Directory containing .md files")
    parser.add_argument(
        "--keys", default="./keys", help="Directory for keys (default: ./keys)"
    )
    parser.add_argument(
        "--tracker",
        default="localhost:1707",
        help="Tracker address (default: localhost:1707)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Seeder port (0 = auto-assign, default: 0)",
    )

    args = parser.parse_args()

    tracker_host, tracker_port = args.tracker.split(":")
    tracker_port = int(tracker_port)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    try:
        asyncio.run(
            main(
                args.uri,
                args.author,
                args.site,
                args.keys,
                tracker_host,
                tracker_port,
                args.port,
            )
        )
    except KeyboardInterrupt:
        print("\n[PUBLISH] Stopped.")