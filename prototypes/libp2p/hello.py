"""
mdp2p libp2p prototype — hello world.

Validates that py-libp2p can carry the core pattern of mdp2p:
  - A listener exposes a custom protocol `/mdp2p/hello/1.0.0`
  - It returns a JSON payload (a tiny mock manifest) signed with an ed25519
    author key that is separate from the libp2p PeerID
  - The dialer receives the payload and verifies the signature locally

This intentionally mirrors the two-identity model (peer identity vs author
identity) that mdp2p would use once migrated to libp2p.
"""

import argparse
import json
import logging
import secrets
import sys

import multiaddr
import trio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.custom_types import TProtocol
from libp2p.network.stream.net_stream import INetStream
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.utils.address_validation import (
    find_free_port,
    get_available_interfaces,
)

logging.basicConfig(level=logging.WARNING)
logging.getLogger("libp2p").setLevel(logging.WARNING)
logging.getLogger("multiaddr").setLevel(logging.WARNING)

PROTOCOL_ID = TProtocol("/mdp2p/hello/1.0.0")
MAX_READ_LEN = 2**20  # 1 MiB is plenty for a hello


# ─── Author identity (ed25519) — independent from libp2p PeerID ────────

def make_author_keypair() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def author_pubkey_bytes(priv: Ed25519PrivateKey) -> bytes:
    return priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def sign_manifest(manifest: dict, priv: Ed25519PrivateKey) -> bytes:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return priv.sign(payload)


def verify_manifest(manifest: dict, signature: bytes, pubkey_bytes: bytes) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(pubkey_bytes)
        payload = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        pub.verify(signature, payload)
        return True
    except Exception:
        return False


# ─── libp2p stream handlers ────────────────────────────────────────────

def build_listener_handler(author_priv: Ed25519PrivateKey):
    pub_bytes = author_pubkey_bytes(author_priv)

    async def handler(stream: INetStream) -> None:
        try:
            manifest = {
                "uri": "hello.world",
                "author": "prototype",
                "version": 1,
                "message": "hello from mdp2p over libp2p",
            }
            signature = sign_manifest(manifest, author_priv)
            payload = {
                "manifest": manifest,
                "signature_hex": signature.hex(),
                "author_pubkey_hex": pub_bytes.hex(),
            }
            await stream.write(json.dumps(payload).encode())
        finally:
            await stream.close()

    return handler


async def dialer_exchange(host, peer_info) -> dict:
    await host.connect(peer_info)
    stream = await host.new_stream(peer_info.peer_id, [PROTOCOL_ID])
    try:
        raw = await stream.read(MAX_READ_LEN)
    finally:
        await stream.close()
    return json.loads(raw.decode())


# ─── Roles ─────────────────────────────────────────────────────────────

async def run_listener(port: int) -> None:
    if port <= 0:
        port = find_free_port()

    author_priv = make_author_keypair()
    host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))

    listen_addrs = get_available_interfaces(port)
    async with host.run(listen_addrs=listen_addrs), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)
        host.set_stream_handler(PROTOCOL_ID, build_listener_handler(author_priv))

        peer_id = host.get_id().to_string()
        print(f"Listener PeerID: {peer_id}")
        print(f"Author pubkey (hex): {author_pubkey_bytes(author_priv).hex()}")
        print("\nAddresses:")
        for addr in host.get_addrs():
            print(f"  {addr}")
        print(
            "\nRun the dialer:\n"
            f"  python hello.py --dial /ip4/127.0.0.1/tcp/{port}/p2p/{peer_id}\n"
        )
        print("Waiting for incoming streams... (Ctrl+C to stop)")
        await trio.sleep_forever()


async def run_dialer(destination: str) -> int:
    host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))
    listen_addrs = get_available_interfaces(find_free_port())

    async with host.run(listen_addrs=listen_addrs), trio.open_nursery() as nursery:
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)

        peer_info = info_from_p2p_addr(multiaddr.Multiaddr(destination))
        print(f"Dialer PeerID: {host.get_id().to_string()}")
        print(f"Dialing: {destination}")

        payload = await dialer_exchange(host, peer_info)

        manifest = payload["manifest"]
        signature = bytes.fromhex(payload["signature_hex"])
        pubkey = bytes.fromhex(payload["author_pubkey_hex"])

        print("\nReceived manifest:")
        print(json.dumps(manifest, indent=2))
        print(f"Author pubkey (hex): {pubkey.hex()}")

        if verify_manifest(manifest, signature, pubkey):
            print("\nSignature OK — author-signed payload transported over libp2p.")
            return 0

        print("\nSignature INVALID!", file=sys.stderr)
        return 1


def main() -> None:
    parser = argparse.ArgumentParser(description="mdp2p libp2p hello-world prototype")
    parser.add_argument("--port", type=int, default=0, help="listen port (listener only)")
    parser.add_argument("--dial", type=str, help="remote multiaddr to dial")
    args = parser.parse_args()

    try:
        if args.dial:
            exit_code = trio.run(run_dialer, args.dial)
            sys.exit(exit_code)
        else:
            trio.run(run_listener, args.port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
