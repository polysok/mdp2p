"""Reviewer-side daemon: identity, registration, inbox polling.

Async-first model: a reviewer does not need to be online when an
assignment is posted. At startup, the daemon registers its opt-in with
current addresses; then it periodically re-registers (heartbeat) and
polls its inbox for new review assignments. Assignments are processed
by a user-supplied callback which may take minutes, hours, or days to
return a verdict — the daemon handles state so assignments aren't
processed more than once.

The actual review decision is the callback's concern. The default,
auto_decline, never emits a verdict and is safe for a peer opted into
reviewer_mode without any UI or policy plugged in yet.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

import trio
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from libp2p.abc import IHost
from libp2p.peer.peerinfo import PeerInfo

from bundle.crypto import (
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_to_b64,
)
from naming import (
    client_attach_review,
    client_list_assignments,
    client_register_reviewer,
)
from review import (
    build_review_record,
    build_reviewer_opt_in,
    sign_review_record,
    sign_reviewer_opt_in,
    verify_review_assignment,
)

logger = logging.getLogger("mdp2p.peer.reviewer")


DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 15 * 60  # 15 min
DEFAULT_POLL_INTERVAL_SECONDS = 5 * 60        # 5 min


@dataclass(frozen=True)
class AssignmentContext:
    """What the callback sees when deciding on an assignment."""

    assignment: dict       # the signed review assignment record
    manifest: dict         # the fetched manifest for the content


@dataclass(frozen=True)
class ReviewVerdict:
    """What the callback returns to emit a signed review."""

    verdict: str           # "ok" | "warn" | "reject"
    comment: str = ""


ReviewerCallback = Callable[[AssignmentContext], Awaitable[Optional[ReviewVerdict]]]
ContentFetcher = Callable[[dict], Awaitable[Optional[dict]]]


async def auto_decline(_ctx: AssignmentContext) -> Optional[ReviewVerdict]:
    """Default callback: never emit a verdict."""
    return None


# ─── Reviewer identity ────────────────────────────────────────────────


def ensure_reviewer_identity(
    key_dir: str,
    name: str = "reviewer",
) -> tuple[Ed25519PrivateKey, str]:
    """Load an existing reviewer keypair from disk, or create a fresh one.

    The keypair is stored alongside the reviewer's other state so a
    restart re-uses the same identity (and therefore the same pool
    selection rank for any given content).
    """
    key_path = Path(key_dir)
    priv_file = key_path / f"{name}.key"
    pub_file = key_path / f"{name}.pub"
    if not (priv_file.exists() and pub_file.exists()):
        generate_keypair(str(key_path), name)
    private_key = load_private_key(str(priv_file))
    public_key_b64 = public_key_to_b64(load_public_key(str(pub_file)))
    return private_key, public_key_b64


# ─── One-shot registration ────────────────────────────────────────────


async def register_reviewer_once(
    host: IHost,
    naming_info: PeerInfo,
    private_key: Ed25519PrivateKey,
    public_key_b64: str,
    peer_id: str,
    addrs: list[str],
    categories: Optional[list[str]] = None,
    timestamp: Optional[int] = None,
) -> bool:
    """Post a single signed reviewer_opt_in record to the naming server.

    Returns True on acceptance. Logs and returns False on any failure —
    the daemon's retry cadence handles transient issues.
    """
    record = build_reviewer_opt_in(
        public_key_b64=public_key_b64,
        peer_id=peer_id,
        addrs=list(addrs),
        categories=categories,
        timestamp=timestamp,
    )
    signature = sign_reviewer_opt_in(record, private_key)

    try:
        resp = await client_register_reviewer(host, naming_info, record, signature)
    except Exception as e:
        logger.warning("register_reviewer RPC failed: %s", e)
        return False

    if resp.get("type") != "ok":
        logger.warning("register_reviewer rejected: %s", resp.get("msg"))
        return False

    logger.info(
        "reviewer registered: pubkey=%s addrs=%d categories=%s",
        public_key_b64[:12],
        len(addrs),
        categories or "any",
    )
    return True


# ─── Local cache of processed assignments ────────────────────────────


def _load_cache(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {"processed": []}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if "processed" not in data or not isinstance(data["processed"], list):
            data["processed"] = []
        return data
    except Exception:
        return {"processed": []}


def _save_cache(cache: dict, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(
        json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8"
    )
    tmp.replace(p)


# ─── Daemon loop ──────────────────────────────────────────────────────


async def run_reviewer_daemon(
    host: IHost,
    naming_info: PeerInfo,
    private_key: Ed25519PrivateKey,
    public_key_b64: str,
    peer_id: str,
    get_addrs: Callable[[], list[str]],
    content_fetcher: ContentFetcher,
    callback: ReviewerCallback = auto_decline,
    cache_path: str = "~/.mdp2p/reviewer_cache.json",
    categories: Optional[list[str]] = None,
    heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """Run the reviewer daemon: heartbeat + inbox polling.

    Exits when the enclosing nursery is cancelled. Intended to be started
    with ``nursery.start_soon(run_reviewer_daemon, ...)`` alongside the
    rest of the peer's tasks.
    """
    expanded_cache = str(Path(cache_path).expanduser())

    # First registration happens before the periodic loop kicks in so the
    # reviewer is visible in the pool as soon as it boots.
    await register_reviewer_once(
        host,
        naming_info,
        private_key,
        public_key_b64,
        peer_id,
        get_addrs(),
        categories,
    )

    async with trio.open_nursery() as nursery:
        nursery.start_soon(
            _heartbeat_loop,
            host,
            naming_info,
            private_key,
            public_key_b64,
            peer_id,
            get_addrs,
            categories,
            heartbeat_interval_seconds,
        )
        nursery.start_soon(
            _poll_loop,
            host,
            naming_info,
            private_key,
            public_key_b64,
            content_fetcher,
            callback,
            expanded_cache,
            poll_interval_seconds,
        )


async def _heartbeat_loop(
    host: IHost,
    naming_info: PeerInfo,
    private_key: Ed25519PrivateKey,
    public_key_b64: str,
    peer_id: str,
    get_addrs: Callable[[], list[str]],
    categories: Optional[list[str]],
    interval_seconds: float,
) -> None:
    while True:
        await trio.sleep(interval_seconds)
        await register_reviewer_once(
            host,
            naming_info,
            private_key,
            public_key_b64,
            peer_id,
            get_addrs(),
            categories,
        )


async def _poll_loop(
    host: IHost,
    naming_info: PeerInfo,
    private_key: Ed25519PrivateKey,
    public_key_b64: str,
    content_fetcher: ContentFetcher,
    callback: ReviewerCallback,
    cache_path: str,
    interval_seconds: float,
) -> None:
    while True:
        try:
            await _poll_once(
                host,
                naming_info,
                private_key,
                public_key_b64,
                content_fetcher,
                callback,
                cache_path,
            )
        except Exception as e:
            logger.warning("reviewer poll cycle failed: %s", e)
        await trio.sleep(interval_seconds)


async def _poll_once(
    host: IHost,
    naming_info: PeerInfo,
    private_key: Ed25519PrivateKey,
    public_key_b64: str,
    content_fetcher: ContentFetcher,
    callback: ReviewerCallback,
    cache_path: str,
) -> None:
    """Single pass of the inbox-polling loop, extracted for testability."""
    listing = await client_list_assignments(host, naming_info, public_key_b64)
    entries = listing.get("records") or []
    if not entries:
        return

    cache = _load_cache(cache_path)
    processed: list[str] = cache.get("processed", [])
    now = int(time.time())
    changed = False

    for entry in entries:
        record = entry.get("record") or {}
        signature = entry.get("signature", "")
        content_key = record.get("content_key", "")

        if not content_key or content_key in processed:
            continue

        ok, err = verify_review_assignment(record, signature, max_drift=None)
        if not ok:
            logger.warning("ignoring invalid assignment signature: %s", err)
            continue

        if int(record.get("deadline", 0)) < now:
            logger.info("skipping expired assignment for %s", content_key[:24])
            processed.append(content_key)
            changed = True
            continue

        try:
            manifest = await content_fetcher(record)
        except Exception as e:
            logger.warning("content fetch failed for %s: %s", content_key[:24], e)
            continue
        if manifest is None:
            logger.warning("content unavailable for %s, will retry later", content_key[:24])
            continue

        ctx = AssignmentContext(assignment=record, manifest=manifest)
        try:
            verdict = await callback(ctx)
        except Exception as e:
            logger.warning("reviewer callback raised for %s: %s", content_key[:24], e)
            continue

        if verdict is not None:
            await _attach_verdict(
                host, naming_info, private_key, public_key_b64, content_key, verdict
            )

        processed.append(content_key)
        changed = True

    if changed:
        cache["processed"] = processed
        _save_cache(cache, cache_path)


async def _attach_verdict(
    host: IHost,
    naming_info: PeerInfo,
    private_key: Ed25519PrivateKey,
    public_key_b64: str,
    content_key: str,
    verdict: ReviewVerdict,
) -> None:
    record = build_review_record(
        content_key=content_key,
        reviewer_pubkey_b64=public_key_b64,
        verdict=verdict.verdict,
        comment=verdict.comment,
    )
    signature = sign_review_record(record, private_key)

    try:
        resp = await client_attach_review(host, naming_info, record, signature)
    except Exception as e:
        logger.warning("attach_review RPC failed for %s: %s", content_key[:24], e)
        return

    if resp.get("type") != "ok":
        logger.warning("attach_review rejected for %s: %s", content_key[:24], resp.get("msg"))
    else:
        logger.info(
            "review attached: %s verdict=%s", content_key[:24], verdict.verdict
        )
