"""Minimal libp2p dial diagnostic — tries to connect to a peer and
prints every protocol-level step. Run against the peer-zero to locate
where the Noise handshake fails.

Usage:
    python scripts/debug_dial.py /ip4/<IP>/tcp/1707/p2p/<PEER_ID>
"""

import logging
import secrets
import sys

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr


async def main(target: str) -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("libp2p").setLevel(logging.DEBUG)
    logging.getLogger("multiaddr").setLevel(logging.INFO)

    host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))
    listen = [multiaddr.Multiaddr("/ip4/127.0.0.1/tcp/0")]

    async with host.run(listen_addrs=listen), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)

        info = info_from_p2p_addr(multiaddr.Multiaddr(target))
        print(f"My PeerID: {host.get_id().to_string()}")
        print(f"Target   : {info.peer_id}")
        print(f"Addresses: {info.addrs}")
        try:
            with trio.move_on_after(20):
                await host.connect(info)
                print("CONNECT OK — fully upgraded secure channel established")
        except Exception as e:
            print(f"CONNECT FAILED: {type(e).__name__}: {e}")

        nursery.cancel_scope.cancel()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: debug_dial.py /ip4/<IP>/tcp/<PORT>/p2p/<PEER_ID>")
        sys.exit(1)
    trio.run(main, sys.argv[1])
