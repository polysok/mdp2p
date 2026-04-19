"""Long-running seeder flow: re-announce every local site in the DHT."""

import logging
import sys
from pathlib import Path

import multiaddr
import trio
from libp2p.peer.peerinfo import info_from_p2p_addr

# Root-level modules (peer) live one directory up.
sys.path.insert(0, str(Path(__file__).parent.parent))

from peer import run_peer

from .config import ClientConfig
from .publish_flow import get_pinstore_path, require_naming

logger = logging.getLogger("mdp2p.serve")

# py-libp2p DHT provider records expire after ~24h, so seeders must refresh.
REANNOUNCE_INTERVAL_SECONDS = 12 * 3600


async def _announce_all(peer, uris: list[str]) -> None:
    """Announce a list of URIs, logging each outcome and isolating failures."""
    for uri in uris:
        try:
            ok = await peer.announce(uri)
            if ok:
                logger.info("announced %s", uri)
            else:
                logger.warning("announce returned false for %s", uri)
        except Exception as e:
            logger.warning("announce raised for %s: %s", uri, e)


async def _periodic_reannounce(peer) -> None:
    """Re-announce all local sites every 12 hours to keep DHT records fresh."""
    while True:
        await trio.sleep(REANNOUNCE_INTERVAL_SECONDS)
        logger.info("periodic re-announce of %d site(s)", len(peer.sites))
        await _announce_all(peer, list(peer.sites.keys()))


async def do_serve(config: ClientConfig) -> None:
    """Run as a foreground seeder: announce all local sites and stay online."""
    maddr = require_naming(config)
    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))

    async with run_peer(
        data_dir=str(config.data_dir),
        port=config.port,
        naming_info=naming_info,
        pinstore_path=get_pinstore_path(),
        bootstrap_multiaddrs=[maddr],
        relay_mode="client",
    ) as peer:
        logger.info("Peer ID: %s", peer.host.get_id().to_string())
        logger.info("seeding %d local site(s)", len(peer.sites))

        await _announce_all(peer, sorted(peer.sites.keys()))

        async with trio.open_nursery() as nursery:
            nursery.start_soon(_periodic_reannounce, peer)
            await trio.sleep_forever()
