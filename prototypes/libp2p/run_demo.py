"""
End-to-end demo: runs a listener and a dialer in the same process
and asserts the signed payload round-trip works.

Exits 0 on success, 1 on failure. No external setup required.
"""

import json
import secrets
import sys

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.utils.address_validation import find_free_port, get_available_interfaces

from hello import (
    PROTOCOL_ID,
    author_pubkey_bytes,
    build_listener_handler,
    dialer_exchange,
    make_author_keypair,
    verify_manifest,
)


async def main() -> int:
    listener_port = find_free_port()
    author_priv = make_author_keypair()
    expected_pub = author_pubkey_bytes(author_priv)

    listener_host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))
    dialer_host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))

    listener_addrs = get_available_interfaces(listener_port)
    dialer_addrs = get_available_interfaces(find_free_port())

    async with (
        listener_host.run(listen_addrs=listener_addrs),
        dialer_host.run(listen_addrs=dialer_addrs),
        trio.open_nursery() as nursery,
    ):
        nursery.start_soon(listener_host.get_peerstore().start_cleanup_task, 60)
        nursery.start_soon(dialer_host.get_peerstore().start_cleanup_task, 60)

        listener_host.set_stream_handler(
            PROTOCOL_ID, build_listener_handler(author_priv)
        )

        listener_peer_id = listener_host.get_id().to_string()
        dest = f"/ip4/127.0.0.1/tcp/{listener_port}/p2p/{listener_peer_id}"
        print(f"[demo] listener at {dest}")
        print(f"[demo] dialer peer: {dialer_host.get_id().to_string()}")
        print(f"[demo] author pubkey: {expected_pub.hex()}")

        peer_info = info_from_p2p_addr(multiaddr.Multiaddr(dest))
        payload = await dialer_exchange(dialer_host, peer_info)

        manifest = payload["manifest"]
        signature = bytes.fromhex(payload["signature_hex"])
        pubkey = bytes.fromhex(payload["author_pubkey_hex"])

        print(f"[demo] received manifest: {json.dumps(manifest)}")

        assert pubkey == expected_pub, "author pubkey mismatch"
        assert verify_manifest(manifest, signature, pubkey), "signature invalid"
        print("[demo] signature OK, author pubkey matches")

        # Tamper test: flipping one bit should break the signature.
        tampered = dict(manifest)
        tampered["message"] = "evil injection"
        assert not verify_manifest(tampered, signature, pubkey), (
            "tampered manifest must fail verification"
        )
        print("[demo] tampered manifest correctly rejected")

        # Cleanly stop background tasks.
        nursery.cancel_scope.cancel()
        return 0


if __name__ == "__main__":
    try:
        sys.exit(trio.run(main))
    except AssertionError as e:
        print(f"[demo] FAILED: {e}", file=sys.stderr)
        sys.exit(1)
