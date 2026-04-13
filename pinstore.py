"""
MDP2P Pinstore — Trust-On-First-Use (TOFU) key pinning for site identities.

Similar to SSH's known_hosts mechanism: the public key of a site is stored
on first contact and verified on subsequent visits. If the key changes,
the connection is refused to prevent MITM attacks.
"""

import json
import logging
import time
from enum import Enum
from pathlib import Path

logger = logging.getLogger("mdp2p.pinstore")


class PinStatus(Enum):
    """Result of checking a key against the pinstore."""

    UNKNOWN = "unknown"
    MATCH = "match"
    MISMATCH = "mismatch"


def load_pinstore(path: str) -> dict:
    """Load the pinstore from a JSON file. Returns empty dict if not found."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_pinstore(pinstore: dict, path: str) -> None:
    """Save the pinstore to a JSON file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(pinstore, indent=2, sort_keys=True), encoding="utf-8")


def check_pin(pinstore: dict, uri: str, public_key_b64: str) -> PinStatus:
    """Check a public key against the pinstore for a given URI."""
    entry = pinstore.get(uri)
    if entry is None:
        return PinStatus.UNKNOWN
    if entry["public_key"] == public_key_b64:
        return PinStatus.MATCH
    return PinStatus.MISMATCH


def pin_key(uri: str, public_key_b64: str, author: str, path: str) -> None:
    """Pin a public key for a URI. Preserves first_seen on update."""
    pinstore = load_pinstore(path)
    now = int(time.time())
    existing = pinstore.get(uri)
    pinstore[uri] = {
        "public_key": public_key_b64,
        "author": author,
        "first_seen": existing["first_seen"] if existing else now,
        "last_seen": now,
    }
    save_pinstore(pinstore, path)


def unpin_key(uri: str, path: str) -> bool:
    """Remove a pinned key. Returns True if removed, False if not found."""
    pinstore = load_pinstore(path)
    if uri not in pinstore:
        return False
    del pinstore[uri]
    save_pinstore(pinstore, path)
    return True


def update_pin_last_seen(uri: str, path: str) -> None:
    """Update the last_seen timestamp for a pinned key."""
    pinstore = load_pinstore(path)
    if uri in pinstore:
        pinstore[uri]["last_seen"] = int(time.time())
        save_pinstore(pinstore, path)
