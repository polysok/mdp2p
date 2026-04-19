"""Helpers for assembling the libp2p host stack used by ``run_peer``.

This module concentrates the plumbing that turns a bare libp2p host into a
production-ready peer: LAN address detection, Circuit Relay v2 wiring, DHT
bootstrap, and relay dialing. Keeping these pieces here lets ``lifecycle.py``
stay focused on orchestration instead of drowning in protocol setup.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import Optional

import multiaddr
from libp2p.abc import IHost
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


def detect_local_ip() -> str:
    """Best-effort detection of the primary LAN IPv4 of this machine.

    Uses the UDP-connect trick: asks the kernel which interface it would
    use to reach a public address, without actually sending any packet.
    Falls back to ``127.0.0.1`` when no routable interface is available
    (offline machine, container with no egress, etc.).
    """
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _default_relay_limits() -> RelayLimits:
    """Conservative limits for a public peer-zero running as a relay.

    100 MiB/reservation and 10 concurrent circuits caps bandwidth so the
    relay cannot be trivially abused as free bandwidth.
    """
    return RelayLimits(
        duration=3600,
        data=100 * 1024 * 1024,
        max_circuit_conns=10,
        max_reservations=20,
    )


async def build_circuit_stack(
    host: IHost,
    relay_mode: str,
    stack: AsyncExitStack,
) -> Optional[CircuitV2Transport]:
    """Install Circuit Relay v2 services on ``host`` according to ``relay_mode``.

    Design choice: this helper takes the caller's ``AsyncExitStack`` and
    enters the background services itself. That keeps lifecycle ownership
    with ``run_peer`` (via its exit stack) while avoiding the boilerplate
    of returning a list of services for the caller to thread into the
    same stack — the coupling would only make the call site noisier.

    Returns the ``CircuitV2Transport`` when ``relay_mode`` is "client" or
    "hop", or ``None`` for "none" (no relay services installed).
    """
    if relay_mode == "none":
        return None

    limits = _default_relay_limits()
    allow_hop = relay_mode == "hop"
    circuit_protocol = CircuitV2Protocol(host, limits=limits, allow_hop=allow_hop)

    roles = RelayRole.STOP | RelayRole.CLIENT
    if allow_hop:
        roles |= RelayRole.HOP
    relay_config = RelayConfig(roles=roles, limits=limits)

    host.set_stream_handler(HOP_PROTOCOL_ID, circuit_protocol._handle_hop_stream)
    host.set_stream_handler(STOP_PROTOCOL_ID, circuit_protocol._handle_stop_stream)
    await stack.enter_async_context(background_trio_service(circuit_protocol))

    relay_transport = CircuitV2Transport(host, circuit_protocol, relay_config)
    if relay_mode == "client":
        discovery = RelayDiscovery(host, auto_reserve=True)
        relay_transport.discovery = discovery
        await stack.enter_async_context(background_trio_service(discovery))
        dcutr = DCUtRProtocol(host)
        await stack.enter_async_context(background_trio_service(dcutr))

    return relay_transport


async def bootstrap_dht(
    host: IHost,
    dht,
    bootstrap_multiaddrs: Optional[list[str]],
    logger: logging.Logger,
) -> None:
    """Seed the DHT routing table by dialing the given bootstrap multiaddrs.

    No-op when ``dht`` is None. Failures are logged but never raised — one
    unreachable bootstrap peer should not abort peer startup.
    """
    if dht is None:
        return
    for maddr in bootstrap_multiaddrs or []:
        try:
            info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))
            host.get_peerstore().add_addrs(info.peer_id, info.addrs, 3600)
            await host.connect(info)
            await dht.routing_table.add_peer(info.peer_id)
            logger.info("bootstrapped DHT via %s", info.peer_id)
        except Exception as e:
            logger.warning("bootstrap to %s failed: %s", maddr, e)


async def connect_relays(
    host: IHost,
    relay_multiaddrs: Optional[list[str]],
    logger: logging.Logger,
) -> None:
    """Dial the given relay multiaddrs so outbound circuits can use them."""
    for maddr in relay_multiaddrs or []:
        try:
            info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))
            host.get_peerstore().add_addrs(info.peer_id, info.addrs, 3600)
            await host.connect(info)
            logger.info("connected to relay %s", info.peer_id)
        except Exception as e:
            logger.warning("relay connect to %s failed: %s", maddr, e)
