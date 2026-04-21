"""Reviewer-side CLI flows: inbox polling and verdict posting.

Mirror of ``publish_flow`` but for the reviewer identity. Uses an
ephemeral libp2p host so the commands work both inside and outside a
running serve daemon.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Optional

import multiaddr
from libp2p.peer.peerinfo import info_from_p2p_addr

# Root-level modules live one directory up.
sys.path.insert(0, str(Path(__file__).parent.parent))

from naming import client_attach_review, client_list_assignments
from peer.reviewer_daemon import ensure_reviewer_identity
from review import (
    build_review_record,
    sign_review_record,
    verify_review_assignment,
)

from .config import ClientConfig
from .publish_flow import ephemeral_host, require_naming


VALID_VERDICTS = ("ok", "warn", "reject")


async def do_list_inbox(config: ClientConfig) -> list[dict]:
    """Return the verified pending assignments for the local reviewer identity.

    "Pending" means: signature valid, deadline not yet reached. Expired and
    forged records are filtered out so the caller can use the list as-is.
    """
    maddr = require_naming(config)
    _priv, pub_b64 = ensure_reviewer_identity(config.reviewer_dir)
    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))

    async with ephemeral_host() as host:
        response = await client_list_assignments(host, naming_info, pub_b64)

    if response.get("type") != "assignments":
        raise RuntimeError(response.get("msg", "unknown inbox error"))

    now = int(time.time())
    pending: list[dict] = []
    for entry in response.get("records") or []:
        record = entry.get("record") or {}
        signature = entry.get("signature", "")
        ok, _ = verify_review_assignment(record, signature, max_drift=None)
        if not ok:
            continue
        if int(record.get("deadline", 0)) < now:
            continue
        pending.append({"record": record, "signature": signature})
    return pending


async def do_attach_review(
    config: ClientConfig,
    content_key: str,
    verdict: str,
    comment: str = "",
) -> None:
    """Sign and post a review record under the local reviewer identity."""
    if verdict not in VALID_VERDICTS:
        raise ValueError(
            f"verdict must be one of {VALID_VERDICTS}, got {verdict!r}"
        )

    maddr = require_naming(config)
    priv, pub_b64 = ensure_reviewer_identity(config.reviewer_dir)
    naming_info = info_from_p2p_addr(multiaddr.Multiaddr(maddr))

    record = build_review_record(
        content_key=content_key,
        reviewer_pubkey_b64=pub_b64,
        verdict=verdict,
        comment=comment,
    )
    signature = sign_review_record(record, priv)

    async with ephemeral_host() as host:
        response = await client_attach_review(host, naming_info, record, signature)

    if response.get("type") != "ok":
        raise RuntimeError(response.get("msg", "unknown attach_review error"))
