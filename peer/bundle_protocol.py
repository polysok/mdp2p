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


# A lookup returns the local site directory for a given uri, or None if absent.
SitesLookup = Callable[[str], Optional[str]]


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
) -> Optional[dict]:
    """Try each seeder in parallel; return the first successful bundle.

    ``host`` and ``relay_transport`` are passed in rather than pulled from
    a Peer instance so this helper can be unit-tested without constructing
    a full Peer. The ``logger`` argument lets callers keep their own
    category-specific logger in the output.
    """
    results: list[Optional[dict]] = [None] * len(seeder_addrs)

    async def try_one(idx: int, addr_str: str) -> None:
        try:
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
                await stream.close()
            if response and response.get("type") == "bundle":
                results[idx] = response
        except Exception as e:
            logger.warning("fetch from %s failed: %s", addr_str, e)

    async with trio.open_nursery() as nursery:
        for idx, addr in enumerate(seeder_addrs):
            nursery.start_soon(try_one, idx, addr)

    for r in results:
        if r is not None:
            return r
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
            msg = await recv_framed_json(stream, MAX_BUNDLE_MSG_SIZE)
            if msg is None:
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
