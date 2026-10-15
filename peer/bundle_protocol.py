"""The ``/mdp2p/bundle/1.0.0`` wire protocol.

Contains the protocol constants, the seeder-side stream handler factory,
and the client-side parallel download helper. These are factored out of
the Peer class so the Peer stays focused on policy (what to fetch, what
to seed) while this module owns the bytes-on-the-wire details.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable, Optional

import multiaddr
import trio
from libp2p.abc import IHost
from libp2p.custom_types import TProtocol
from libp2p.network.stream.net_stream import INetStream
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.relay.circuit_v2.transport import CircuitV2Transport

from bundle import bundle_to_dict, load_bundle
from wire import recv_framed_json, send_framed_json

logger = logging.getLogger("mdp2p.peer.protocol")

BUNDLE_PROTOCOL = TProtocol("/mdp2p/bundle/1.0.0")
MAX_BUNDLE_MSG_SIZE = 100 * 1024 * 1024  # 100 MiB — bundle cap is 50 MiB in bundle.py
REQUEST_READ_TIMEOUT = 10.0  # seconds a seeder waits for the request frame
MAX_REQUEST_SIZE = 64 * 1024  # get_bundle / get_manifest requests
DEFAULT_SEEDER_TIMEOUT = 120.0  # seconds to dial + download from one seeder
MAX_PARALLEL_SEEDERS = 4  # bounds memory: each in-flight bundle can be ~100 MiB


# A lookup returns the local site directory for a given uri, or None if absent.
SitesLookup = Callable[[str], Optional[str]]

# A validator returns None when a downloaded bundle is acceptable, or a
# human-readable reason when it must be discarded.
BundleValidator = Callable[[dict], Optional[str]]


async def _send_error(stream: INetStream, message: str) -> None:
    try:
        await send_framed_json(
            stream, {"type": "error", "msg": message}, MAX_BUNDLE_MSG_SIZE
        )
    except Exception:
        pass


async def try_download_from_seeders(
    host: IHost,
    relay_transport: Optional[CircuitV2Transport],
    uri: str,
    seeder_addrs: list[str],
    logger: logging.Logger,
    validate: Optional[BundleValidator] = None,
    timeout: float = DEFAULT_SEEDER_TIMEOUT,
) -> Optional[dict]:
    """Try seeders in parallel; return the first bundle that passes ``validate``.

    Provider records in the DHT are unauthenticated, so any peer can claim
    to seed a uri. Validating each response individually means a single
    hostile or broken seeder cannot make the whole fetch fail; the first
    valid response cancels the remaining downloads, and every attempt is
    bounded by ``timeout`` so a stalling seeder cannot hang the caller.

    ``host`` and ``relay_transport`` are passed in rather than pulled from
    a Peer instance so this helper can be unit-tested without constructing
    a full Peer. The ``logger`` argument lets callers keep their own
    category-specific logger in the output.
    """
    winner: list[dict] = []
    limiter = trio.CapacityLimiter(MAX_PARALLEL_SEEDERS)

    async def try_one(addr_str: str, cancel_scope: trio.CancelScope) -> None:
        async with limiter:
            with trio.move_on_after(timeout) as attempt:
                response = await _download_one(host, relay_transport, uri, addr_str)
            if attempt.cancelled_caught:
                logger.warning("fetch from %s timed out after %.0fs", addr_str, timeout)
                return
        if response is None or winner:
            return
        reason = validate(response) if validate is not None else None
        if reason is not None:
            logger.warning("discarding bundle from %s: %s", addr_str, reason)
            return
        winner.append(response)
        cancel_scope.cancel()

    async def guarded(addr_str: str, cancel_scope: trio.CancelScope) -> None:
        try:
            await try_one(addr_str, cancel_scope)
        except Exception as e:
            logger.warning("fetch from %s failed: %s", addr_str, e)

    async with trio.open_nursery() as nursery:
        for addr in seeder_addrs:
            nursery.start_soon(guarded, addr, nursery.cancel_scope)

    return winner[0] if winner else None


async def _download_one(
    host: IHost,
    relay_transport: Optional[CircuitV2Transport],
    uri: str,
    addr_str: str,
) -> Optional[dict]:
    """Dial one seeder and request the bundle. Returns the raw response or None."""
    maddr = multiaddr.Multiaddr(addr_str)
    info = info_from_p2p_addr(maddr)
    if "/p2p-circuit/" in addr_str and relay_transport is not None:
        # CircuitV2Transport needs the destination peer in the
        # peerstore to complete the dial, even though the circuit
        # address already carries the peer_id.
        host.get_peerstore().add_addrs(info.peer_id, [maddr], 3600)
        await relay_transport.dial(maddr)
    else:
        await host.connect(info)
    stream = await host.new_stream(info.peer_id, [BUNDLE_PROTOCOL])
    try:
        await send_framed_json(
            stream, {"type": "get_bundle", "uri": uri}, MAX_BUNDLE_MSG_SIZE
        )
        response = await recv_framed_json(stream, MAX_BUNDLE_MSG_SIZE)
    finally:
        # Shielded so a timeout or a sibling's win still releases the
        # stream, but bounded so a peer that never ACKs cannot stall us.
        with trio.CancelScope(shield=True), trio.move_on_after(2):
            await stream.close()
    if isinstance(response, dict) and response.get("type") == "bundle":
        return response
    return None


def make_bundle_handler(
    sites_lookup: SitesLookup,
) -> Callable[[INetStream], Awaitable[None]]:
    """Build the stream handler for ``BUNDLE_PROTOCOL``.

    The handler is decoupled from Peer state: the caller supplies a
    ``sites_lookup`` callable which returns the local site directory for
    a uri (or None if this peer does not seed it). This makes the handler
    testable without a full Peer instance and also keeps the handler's
    dependencies explicit.
    """

    async def handler(stream: INetStream) -> None:
        try:
            # Requests are tiny; cap both their size and how long a client
            # may take to send one, so idle streams cannot pin seeder memory.
            msg = None
            with trio.move_on_after(REQUEST_READ_TIMEOUT):
                msg = await recv_framed_json(stream, MAX_REQUEST_SIZE)
            if not isinstance(msg, dict):
                return
            msg_type = msg.get("type", "")
            uri = msg.get("uri", "")

            site_dir = sites_lookup(uri)
            if site_dir is None:
                await _send_error(stream, f"site '{uri}' not found")
                return

            if msg_type == "get_bundle":
                data = bundle_to_dict(site_dir)
                data["type"] = "bundle"
                await send_framed_json(stream, data, MAX_BUNDLE_MSG_SIZE)
            elif msg_type == "get_manifest":
                manifest, signature = load_bundle(site_dir)
                await send_framed_json(
                    stream,
                    {
                        "type": "manifest",
                        "manifest": manifest,
                        "signature": signature,
                    },
                    MAX_BUNDLE_MSG_SIZE,
                )
            else:
                await _send_error(stream, f"unknown type: {msg_type}")
        except Exception as e:
            logger.exception("bundle handler error")
            await _send_error(stream, f"internal error: {e}")
        finally:
            await stream.close()

    return handler
