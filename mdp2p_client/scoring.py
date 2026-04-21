"""Read-only scoring helpers for the TUI.

Once a fetch has run, the peer caches the review attachments it saw at
``{site_dir}/attachments.json``. This module turns that cache into a
ScoreResult without any network I/O — the TUI can show a badge for
every site in the sidebar at startup without spinning up a libp2p
client.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

from bundle import compute_content_key, load_bundle
from review import verify_review_record
from trust import (
    Policy,
    ScoreResult,
    Signal,
    default_policy,
    load_policy,
    load_store,
    score_content,
)

logger = logging.getLogger("mdp2p.scoring")


DEFAULT_ATTACHMENTS_FILE = "attachments.json"


def score_from_cache(
    site_dir: str,
    policy_path: Optional[str] = None,
    trust_store_path: Optional[str] = None,
) -> ScoreResult:
    """Score a locally-cached site using its saved review attachments.

    Returns a neutral ScoreResult (score=0, decision="show") when no
    attachments file is present — the content is simply "unreviewed",
    which is the default state before reviewers have had time to respond.
    """
    signals = _load_signals(site_dir)
    policy = load_policy(policy_path) if policy_path else default_policy()
    store = load_store(trust_store_path) if trust_store_path else {"peers": {}}
    return score_content(signals, store, policy)


def _load_signals(site_dir: str) -> list[Signal]:
    """Read, verify, and convert cached attachments into scorer Signals."""
    attachments_path = Path(site_dir) / DEFAULT_ATTACHMENTS_FILE
    if not attachments_path.exists():
        return []

    try:
        raw = json.loads(attachments_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("could not parse %s: %s", attachments_path, e)
        return []

    expected_key = raw.get("content_key")
    if not expected_key:
        # Backwards compat: fall back to recomputing from manifest.
        expected_key = _recompute_content_key(site_dir)
        if not expected_key:
            return []

    entries = raw.get("records") or []
    signals: list[Signal] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        record = entry.get("record")
        signature = entry.get("signature", "")
        if not isinstance(record, dict) or not signature:
            continue
        ok, _ = verify_review_record(record, signature, max_drift=None)
        if not ok:
            continue
        if record.get("content_key") != expected_key:
            continue
        signals.append(
            Signal(
                kind="review",
                content_key=expected_key,
                source_pubkey=record.get("reviewer_public_key", ""),
                verdict=record.get("verdict", "ok"),
                reason=record.get("comment", ""),
                timestamp=int(record.get("timestamp", 0)),
            )
        )
    return signals


def _recompute_content_key(site_dir: str) -> Optional[str]:
    try:
        manifest, _ = load_bundle(site_dir)
    except Exception:
        return None
    uri = manifest.get("uri", "")
    pubkey = manifest.get("public_key", "")
    if not uri or not pubkey:
        return None
    return compute_content_key(uri, pubkey)
