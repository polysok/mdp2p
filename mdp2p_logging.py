"""Shared logging helpers for mdp2p CLIs."""

from __future__ import annotations

import logging

# py-libp2p loggers that emit routine retry noise on stale DHT peer records.
# These errors fire for ghosts (peers that registered then went offline) and
# don't affect the local publish/fetch flow, so we silence them across CLIs.
_NOISY_LIBP2P_LOGGERS = (
    "libp2p.transport.tcp",
    "libp2p.kad_dht.peer_routing",
    "libp2p.host.basic_host",
)


def silence_libp2p_noise() -> None:
    """Raise the level of chatty py-libp2p loggers to CRITICAL."""
    for name in _NOISY_LIBP2P_LOGGERS:
        logging.getLogger(name).setLevel(logging.CRITICAL)
