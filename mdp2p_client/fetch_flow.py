"""Shared fetch logic used by the `mdp2p fetch` CLI and the TUI reader."""

import sys
from pathlib import Path
from typing import Optional

import multiaddr
from libp2p.peer.peerinfo import info_from_p2p_addr

# Root-level modules (peer) live one directory up.
sys.path.insert(0, str(Path(__file__).parent.parent))

from peer import run_peer

from .config import ClientConfig
from .formatting import strip_uri_scheme
from .publish_flow import get_pinstore_path, require_naming


async def do_fetch(
    config: ClientConfig,
    uri: str,
    naming_multiaddr: Optional[str] = None,
    announce_after: bool = False,
) -> bool:
    """Resolve, download and verify a site. Returns True on success.

    ``announce_after`` defaults to False because short-lived CLI/TUI
    invocations exit immediately — advertising as a DHT provider would
    leave a stale record behind. Callers that stay online as seeders
    should pass True.
    """
    uri = strip_uri_scheme(uri)
    maddr = naming_multiaddr or require_naming(config)
    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))

    async with run_peer(
        data_dir=str(config.data_dir),
        port=0,
        naming_info=naming_info,
        pinstore_path=get_pinstore_path(),
        bootstrap_multiaddrs=[maddr],
    ) as peer:
        return await peer.fetch_site(uri, announce_after=announce_after)
