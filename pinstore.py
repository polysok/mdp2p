"""
MDP2P Pinstore — Trust-On-First-Use (TOFU) key pinning for site identities.

Similar to SSH's known_hosts mechanism: the public key of a site is stored
on first contact and verified on subsequent visits. If the key changes,
the connection is refused to prevent MITM attacks.
"""

import json
import logging
import os
import time
from enum import Enum
from pathlib import Path
from typing import Optional

logger = logging.getLogger("mdp2p.pinstore")


class PinStatus(Enum):
    """Result of checking a key against the pinstore."""

    UNKNOWN = "unknown"
    MATCH = "match"
    MISMATCH = "mismatch"
    ROLLBACK = "rollback"


def load_pinstore(path: str) -> dict:
    """Load the pinstore from a JSON file. Returns empty dict if not found."""
    p = Path(path)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def save_pinstore(pinstore: dict, path: str) -> None:
    """Save the pinstore atomically (a crash never leaves a truncated file)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(pinstore, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def check_pin(
    pinstore: dict,
    uri: str,
    public_key_b64: str,
    record_timestamp: Optional[int] = None,
) -> PinStatus:
    """Check a public key (and optionally record freshness) for a given URI.

    When ``record_timestamp`` is given and the pin remembers a newer naming
    record, ROLLBACK is returned: a naming server is replaying an older,
    still validly-signed record (e.g. to resurrect a retracted version).
    """
    entry = pinstore.get(uri)
    if entry is None:
        return PinStatus.UNKNOWN
    if entry["public_key"] != public_key_b64:
        return PinStatus.MISMATCH
    seen = entry.get("record_timestamp")
    if record_timestamp is not None and seen is not None:
        if int(record_timestamp) < int(seen):
            return PinStatus.ROLLBACK
    return PinStatus.MATCH


def pin_key(
    uri: str,
    public_key_b64: str,
    author: str,
    path: str,
    record_timestamp: Optional[int] = None,
) -> None:
    """Pin a public key for a URI. Preserves first_seen on update."""
    pinstore = load_pinstore(path)
    now = int(time.time())
    existing = pinstore.get(uri)
    entry = {
        "public_key": public_key_b64,
        "author": author,
        "first_seen": existing["first_seen"] if existing else now,
        "last_seen": now,
    }
    if record_timestamp is not None:
        entry["record_timestamp"] = int(record_timestamp)
    pinstore[uri] = entry
    save_pinstore(pinstore, path)


def unpin_key(uri: str, path: str) -> bool:
    """Remove a pinned key. Returns True if removed, False if not found."""
    pinstore = load_pinstore(path)
    if uri not in pinstore:
        return False
    del pinstore[uri]
    save_pinstore(pinstore, path)
    return True


def update_pin_last_seen(
    uri: str, path: str, record_timestamp: Optional[int] = None
) -> None:
    """Update last_seen, and raise the high-water mark of record timestamps."""
    pinstore = load_pinstore(path)
    if uri in pinstore:
        entry = pinstore[uri]
        entry["last_seen"] = int(time.time())
        if record_timestamp is not None:
            entry["record_timestamp"] = max(
                int(record_timestamp), int(entry.get("record_timestamp", 0))
            )
        save_pinstore(pinstore, path)
