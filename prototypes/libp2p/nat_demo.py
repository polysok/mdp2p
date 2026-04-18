"""
Prototype 3 — NAT traversal via Circuit Relay v2, with DCUtR upgrade attempt.

Simulates the real-world topology of mdp2p once deployed:

  [relay]  — a publicly reachable peer running HOP (like a public IPFS relay)
    │
    ├── [listener] — a NAT'd seeder, reserves a slot on the relay so it's
    │                reachable through /p2p-circuit addresses
    │
    └── [dialer]   — a NAT'd visitor who wants to fetch content from the
                     listener; connects via the relay, then attempts DCUtR
                     to upgrade to a direct connection.

The demo runs all three peers in the same process and exchanges an
ed25519-signed mock manifest over the circuit. Everything is on localhost,
so DCUtR can't actually "punch" anything meaningful — but the plumbing
(HOP/STOP reservations, circuit dialing, DCUtR handshake) is exercised.

Exits 0 on success, 1 on failure.
"""

import json
import secrets
import sys
from typing import cast

import multiaddr
import trio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.custom_types import TProtocol
from libp2p.host.basic_host import BasicHost
from libp2p.network.stream.net_stream import INetStream
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.relay.circuit_v2.config import RelayConfig, RelayRole
from libp2p.relay.circuit_v2.dcutr import DCUtRProtocol
from libp2p.relay.circuit_v2.discovery import RelayDiscovery
from libp2p.relay.circuit_v2.protocol import (
    PROTOCOL_ID as HOP_PROTOCOL_ID,
    STOP_PROTOCOL_ID,
    CircuitV2Protocol,
)
from libp2p.relay.circuit_v2.resources import RelayLimits
from libp2p.relay.circuit_v2.transport import CircuitV2Transport
from libp2p.tools.async_service import background_trio_service
from libp2p.utils.address_validation import find_free_port

from hello import (
    author_pubkey_bytes,
    build_listener_handler,
    make_author_keypair,
    verify_manifest,
)

APP_PROTOCOL = TProtocol("/mdp2p/hello/1.0.0")
MAX_READ_LEN = 2**16


def make_limits() -> RelayLimits:
    return RelayLimits(
        duration=3600,
        data=10 * 1024 * 1024,
        max_circuit_conns=10,
        max_reservations=5,
    )


async def run_relay(port: int, ready: trio.Event, stop: trio.Event) -> None:
    """Publicly reachable HOP relay."""
    host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))
    protocol = CircuitV2Protocol(host, limits=make_limits(), allow_hop=True)
    config = RelayConfig(
        roles=RelayRole.HOP | RelayRole.STOP | RelayRole.CLIENT,
        limits=make_limits(),
    )

    listen = multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{port}")
    async with host.run(listen_addrs=[listen]):
        host.set_stream_handler(HOP_PROTOCOL_ID, protocol._handle_hop_stream)
        host.set_stream_handler(STOP_PROTOCOL_ID, protocol._handle_stop_stream)
        async with background_trio_service(protocol):
            CircuitV2Transport(host, protocol, config)
            relay_maddr = f"/ip4/127.0.0.1/tcp/{port}/p2p/{host.get_id().to_string()}"
            print(f"[relay]    running at {relay_maddr}")
            # Stash what the other tasks need.
            run_relay.maddr = relay_maddr  # type: ignore[attr-defined]
            run_relay.peer_id = host.get_id()  # type: ignore[attr-defined]
            ready.set()
            await stop.wait()


async def run_listener(
    port: int,
    relay_maddr: str,
    author_priv: Ed25519PrivateKey,
    ready: trio.Event,
    stop: trio.Event,
) -> None:
    """NAT'd seeder — reserves a slot on the relay, serves mdp2p bundles."""
    host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))
    protocol = CircuitV2Protocol(host, limits=make_limits(), allow_hop=False)
    config = RelayConfig(
        roles=RelayRole.STOP | RelayRole.CLIENT, limits=make_limits()
    )
    dcutr = DCUtRProtocol(host)

    listen = multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{port}")
    async with host.run(listen_addrs=[listen]):
        host.set_stream_handler(APP_PROTOCOL, build_listener_handler(author_priv))
        host.set_stream_handler(HOP_PROTOCOL_ID, protocol._handle_hop_stream)
        host.set_stream_handler(STOP_PROTOCOL_ID, protocol._handle_stop_stream)

        async with background_trio_service(protocol):
            transport = CircuitV2Transport(host, protocol, config)
            discovery = RelayDiscovery(host, auto_reserve=True)
            transport.discovery = discovery
            async with background_trio_service(discovery):
                async with background_trio_service(dcutr):
                    relay_info = info_from_p2p_addr(multiaddr.Multiaddr(relay_maddr))
                    await host.connect(relay_info)
                    print(f"[listener] connected to relay {relay_info.peer_id}")
                    await trio.sleep(2)  # let RelayDiscovery reserve a slot
                    run_listener.peer_id = host.get_id()  # type: ignore[attr-defined]
                    print(f"[listener] ready, peer_id={host.get_id()}")
                    ready.set()
                    await stop.wait()


async def run_dialer(
    relay_maddr: str,
    listener_peer_id: ID,
    expected_author_pub: bytes,
    result: dict,
) -> None:
    """NAT'd visitor — dials the listener through the relay circuit."""
    host = new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))
    protocol = CircuitV2Protocol(host, limits=make_limits(), allow_hop=False)
    config = RelayConfig(
        roles=RelayRole.STOP | RelayRole.CLIENT, limits=make_limits()
    )
    dcutr = DCUtRProtocol(host)

    listen = multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{find_free_port()}")
    async with host.run(listen_addrs=[listen]):
        host.set_stream_handler(HOP_PROTOCOL_ID, protocol._handle_hop_stream)
        host.set_stream_handler(STOP_PROTOCOL_ID, protocol._handle_stop_stream)

        async with background_trio_service(protocol):
            transport = CircuitV2Transport(host, protocol, config)
            discovery = RelayDiscovery(host, auto_reserve=False)
            transport.discovery = discovery
            async with background_trio_service(discovery):
                async with background_trio_service(dcutr):
                    relay_info = info_from_p2p_addr(multiaddr.Multiaddr(relay_maddr))
                    await host.connect(relay_info)
                    print(f"[dialer]   connected to relay {relay_info.peer_id}")

                    circuit_addr = multiaddr.Multiaddr(
                        f"{relay_maddr}/p2p-circuit/p2p/{listener_peer_id}"
                    )
                    print(f"[dialer]   dialing circuit: {circuit_addr}")
                    conn = await transport.dial(circuit_addr)
                    print(f"[dialer]   circuit OK: {conn}")

                    # Attempt DCUtR upgrade (best-effort).
                    await dcutr.event_started.wait()
                    try:
                        upgraded = await dcutr.initiate_hole_punch(listener_peer_id)
                        print(f"[dialer]   DCUtR upgrade: {'OK' if upgraded else 'skipped/failed'}")
                    except Exception as e:
                        print(f"[dialer]   DCUtR raised: {e!r}")
                        upgraded = False

                    # Open an application stream — reuses the circuit (or the
                    # direct connection if DCUtR succeeded).
                    stream = await host.new_stream(listener_peer_id, [APP_PROTOCOL])
                    raw = await stream.read(MAX_READ_LEN)
                    await stream.close()

                    payload = json.loads(raw.decode())
                    manifest = payload["manifest"]
                    signature = bytes.fromhex(payload["signature_hex"])
                    author_pub = bytes.fromhex(payload["author_pubkey_hex"])

                    assert author_pub == expected_author_pub, "author pubkey mismatch"
                    assert verify_manifest(manifest, signature, author_pub), (
                        "signature invalid"
                    )
                    print(f"[dialer]   manifest received & verified: {manifest['message']}")

                    result["ok"] = True
                    result["dcutr"] = upgraded


async def main() -> int:
    relay_port = find_free_port()
    listener_port = find_free_port()

    author_priv = make_author_keypair()
    expected_pub = author_pubkey_bytes(author_priv)

    relay_ready = trio.Event()
    listener_ready = trio.Event()
    stop_signal = trio.Event()
    result: dict = {"ok": False, "dcutr": False}

    async with trio.open_nursery() as nursery:
        nursery.start_soon(run_relay, relay_port, relay_ready, stop_signal)
        await relay_ready.wait()

        relay_maddr = run_relay.maddr  # type: ignore[attr-defined]

        nursery.start_soon(
            run_listener,
            listener_port,
            relay_maddr,
            author_priv,
            listener_ready,
            stop_signal,
        )
        await listener_ready.wait()

        listener_peer_id = run_listener.peer_id  # type: ignore[attr-defined]

        with trio.move_on_after(30) as scope:
            await run_dialer(
                relay_maddr, listener_peer_id, expected_pub, result
            )
        if scope.cancelled_caught:
            print("[demo] dialer timed out", file=sys.stderr)

        stop_signal.set()

    if result["ok"]:
        print("\n[demo] SUCCESS — circuit relay transported signed manifest")
        print(f"[demo] DCUtR upgrade: {'YES' if result['dcutr'] else 'no (relayed)'}")
        return 0
    print("\n[demo] FAILED", file=sys.stderr)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(trio.run(main))
    except AssertionError as e:
        print(f"[demo] assertion failed: {e}", file=sys.stderr)
        sys.exit(1)
