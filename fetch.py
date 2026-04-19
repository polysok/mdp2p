#!/usr/bin/env python3
"""
MDP2P Fetch — visitor-side CLI to resolve, download and render a site.

Acts as a fresh visitor:
  1. Connects to the naming server to resolve md://<uri> → (author_pubkey, manifest_ref)
  2. Bootstraps the DHT through the same peer (so find_providers works)
  3. Discovers seeders in the DHT and downloads the bundle
  4. Verifies signature + manifest binding + file integrity
  5. TOFU-pins the author key
  6. Optionally renders the Markdown to stdout
  7. Becomes a seeder itself while the process stays alive

Usage:
    # List all sites registered on the naming server
    python fetch.py --list --naming /dns4/relay.mdp2p.net/tcp/1707/p2p/<PEER_ZERO_ID>

    # Fetch a specific site and render it
    python fetch.py --uri root \\
        --naming /dns4/relay.mdp2p.net/tcp/1707/p2p/<PEER_ZERO_ID>

    # Fetch and stay online as a seeder
    python fetch.py --uri root --seed \\
        --naming /dns4/relay.mdp2p.net/tcp/1707/p2p/<PEER_ZERO_ID>
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

import multiaddr
import trio
from libp2p.peer.peerinfo import info_from_p2p_addr

sys.path.insert(0, str(Path(__file__).parent))

from mdp2p_logging import silence_libp2p_noise
from naming import client_list
from peer import run_peer


async def list_sites(naming_multiaddr: str) -> int:
    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(naming_multiaddr))
    with tempfile.TemporaryDirectory() as tmp:
        async with run_peer(
            data_dir=tmp,
            port=0,
            naming_info=naming_info,
            enable_dht=False,  # list only talks to naming, no DHT needed
        ) as peer:
            response = await client_list(peer.host, naming_info)
            if response.get("type") != "names":
                print(f"error: {response.get('msg')}", file=sys.stderr)
                return 1

            records = response.get("records", [])
            if not records:
                print("(no sites registered)")
                return 0

            print(f"{len(records)} site(s) registered:")
            for r in records:
                pub = r.get("public_key", "")[:16]
                print(
                    f"  md://{r.get('uri'):<24} "
                    f"author={r.get('author'):<16} "
                    f"pubkey={pub}... "
                    f"manifest={r.get('manifest_ref', '')[:12]}..."
                )
    return 0


async def fetch(
    naming_multiaddr: str,
    uri: str,
    data_dir: str,
    pinstore_path: str,
    render: bool,
    seed_forever: bool,
) -> int:
    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(naming_multiaddr))

    async with run_peer(
        data_dir=data_dir,
        port=0,
        naming_info=naming_info,
        pinstore_path=pinstore_path,
        bootstrap_multiaddrs=[naming_multiaddr],
    ) as peer:
        print(f"[FETCH] Peer ID     : {peer.host.get_id().to_string()}")
        print(f"[FETCH] Resolving md://{uri}…")
        # Only announce ourselves as a provider if the caller intends to
        # stay online; one-shot fetches exit immediately and would
        # otherwise leave ghost records in the DHT.
        ok = await peer.fetch_site(uri, announce_after=seed_forever)
        if not ok:
            print(f"[FETCH] Failed to fetch {uri}", file=sys.stderr)
            return 1

        print(f"[FETCH] Downloaded to: {peer.sites[uri]}")

        if render:
            print(peer.render_site(uri))

        if seed_forever:
            print("[FETCH] Seeding. Press Ctrl+C to stop.")
            await trio.sleep_forever()

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="MDP2P visitor client")
    parser.add_argument(
        "--naming",
        required=True,
        help="Naming server multiaddr (e.g. /dns4/relay.mdp2p.net/tcp/1707/p2p/12D3Koo...)",
    )
    parser.add_argument("--uri", help="Site URI to fetch (e.g. root)")
    parser.add_argument("--list", action="store_true", help="List all registered sites")
    parser.add_argument(
        "--data-dir",
        default="./fetch_data",
        help="Local cache for downloaded sites (default: ./fetch_data)",
    )
    parser.add_argument(
        "--pinstore",
        default=str(Path.home() / ".mdp2p" / "known_keys.json"),
        help="TOFU pinstore path",
    )
    parser.add_argument(
        "--render",
        action="store_true",
        help="After fetch, render the Markdown site to stdout",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="After fetch, keep the process running as a seeder",
    )
    args = parser.parse_args()

    if not args.list and not args.uri:
        parser.error("either --list or --uri must be provided")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    silence_libp2p_noise()

    try:
        if args.list:
            exit_code = trio.run(list_sites, args.naming)
        else:
            exit_code = trio.run(
                fetch,
                args.naming,
                args.uri,
                args.data_dir,
                args.pinstore,
                args.render,
                args.seed,
            )
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n[FETCH] Stopped.")
        sys.exit(0)


if __name__ == "__main__":
    main()
