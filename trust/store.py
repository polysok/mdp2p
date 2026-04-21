"""
MDP2P Trust Store — Persistent per-peer trust state.

Stores, per peer public key:
  - explicit_trust: user-set weight (None when not overridden)
  - confirmed_signals: count of signals this peer emitted that were later
    corroborated by others
  - disputed_signals: count of signals contested or proven wrong
  - last_seen: unix timestamp of the most recent observation

Persisted as JSON at `~/.mdp2p/trust.json`, mirroring the style of
`pinstore.py`. Purely local — this file never leaves the user's machine.
"""

import json
import time
from pathlib import Path
from typing import Optional


def load_store(path: str) -> dict:
    """Load the trust store from a JSON file. Returns empty structure if missing."""
    p = Path(path)
    if not p.exists():
        return {"peers": {}}
    data = json.loads(p.read_text(encoding="utf-8"))
    if "peers" not in data or not isinstance(data["peers"], dict):
        data["peers"] = {}
    return data


def save_store(store: dict, path: str) -> None:
    """Save the trust store to a JSON file. Creates parent dirs as needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(store, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def get_peer(store: dict, pubkey: str) -> dict:
    """Return the record for a peer, creating it with neutral defaults if absent.

    Mutates `store` in place when the peer is new so subsequent reads are stable.
    """
    peers = store.setdefault("peers", {})
    record = peers.get(pubkey)
    if record is None:
        record = {
            "explicit_trust": None,
            "confirmed_signals": 0,
            "disputed_signals": 0,
            "last_seen": 0,
        }
        peers[pubkey] = record
    return record


def record_confirmed(store: dict, pubkey: str, now: Optional[int] = None) -> None:
    """Increment the confirmed-signals counter for a peer."""
    record = get_peer(store, pubkey)
    record["confirmed_signals"] = int(record.get("confirmed_signals", 0)) + 1
    record["last_seen"] = now if now is not None else int(time.time())


def record_disputed(store: dict, pubkey: str, now: Optional[int] = None) -> None:
    """Increment the disputed-signals counter for a peer."""
    record = get_peer(store, pubkey)
    record["disputed_signals"] = int(record.get("disputed_signals", 0)) + 1
    record["last_seen"] = now if now is not None else int(time.time())


def set_explicit_trust(store: dict, pubkey: str, value: Optional[float]) -> None:
    """Set or clear a user-defined weight override for a peer.

    Passing `None` clears the override and lets the learned weight take over.
    """
    record = get_peer(store, pubkey)
    record["explicit_trust"] = None if value is None else float(value)
