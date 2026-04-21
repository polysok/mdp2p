"""Long-running seeder flow: re-announce every local site in the DHT."""

import logging
import sys
from pathlib import Path
from typing import Optional

import multiaddr
import trio
from libp2p.peer.peerinfo import info_from_p2p_addr

# Root-level modules (peer) live one directory up.
sys.path.insert(0, str(Path(__file__).parent.parent))

from bundle import load_bundle
from peer import run_peer
from peer.reviewer_daemon import (
    ensure_reviewer_identity,
    run_reviewer_daemon,
)

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


async def _content_fetcher_for(peer):
    """Build a callable that fetches the manifest for an assignment.

    Uses the peer's own fetch_site() to download and verify the content,
    then reads the stored manifest back from disk. Returns None if the
    fetch fails; the daemon will retry on the next poll cycle.
    """
    async def fetch(assignment: dict) -> Optional[dict]:
        uri = assignment.get("uri", "")
        if not uri:
            return None
        ok = await peer.fetch_site(uri, announce_after=False)
        if not ok:
            return None
        site_dir = peer.sites.get(uri)
        if site_dir is None:
            return None
        try:
            manifest, _ = load_bundle(site_dir)
            return manifest
        except Exception:
            return None
    return fetch


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

            if config.reviewer_mode:
                private_key, public_key_b64 = ensure_reviewer_identity(
                    config.reviewer_dir
                )
                peer_id_str = peer.host.get_id().to_string()
                cache_path = str(Path(config.reviewer_dir) / "cache.json")
                fetcher = await _content_fetcher_for(peer)
                categories = list(config.reviewer_categories) or None
                logger.info(
                    "reviewer mode enabled: pubkey=%s categories=%s",
                    public_key_b64[:12],
                    categories or "any",
                )

                async def _reviewer_entrypoint(
                    _host=peer.host,
                    _naming=naming_info,
                    _priv=private_key,
                    _pub=public_key_b64,
                    _peer_id=peer_id_str,
                    _get_addrs=lambda: list(peer.addrs),
                    _fetcher=fetcher,
                    _cache=cache_path,
                    _categories=categories,
                ) -> None:
                    from peer.reviewer_daemon import auto_decline
                    await run_reviewer_daemon(
                        _host,
                        _naming,
                        _priv,
                        _pub,
                        _peer_id,
                        _get_addrs,
                        _fetcher,
                        auto_decline,
                        _cache,
                        _categories,
                    )

                nursery.start_soon(_reviewer_entrypoint)

            await trio.sleep_forever()
